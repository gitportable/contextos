"""The single ingestion path. Live events, history sync, reconcile and the demo all go through ingest_message(),
so a message is scored, de-duplicated and turned into reminders/suggestions exactly one way."""
import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import integrations
from app.config import settings
from app.models import (OutboundAction, Reminder, SlackConnection, SlackConversation, SlackMessage, SlackUser, SuggestedEvent, utcnow)
from app.priority import extract_meeting_suggestion, extract_reminder_due, score_message
from app.projects import assign_project
from app.trace import log_event
from app.slack_text import (conv_type_from_channel_type, has_broadcast, mentioned_user_ids, mentions_user,
                            normalize_text, ts_to_datetime)

log = logging.getLogger("contextos.pipeline")
# Slack subtypes that carry real human/bot content. Joins, topic changes, etc. are ignored.
ALLOWED_SUBTYPES = {None, "thread_broadcast", "file_share", "me_message", "bot_message"}
STALE_AFTER = timedelta(hours=24)  # don't create reminders that were already overdue by more than a day


@dataclass
class IngestResult:
    action: str  # created | updated | duplicate | ignored | deleted
    message: SlackMessage | None = None
    reason: str | None = None


def load_names(db: Session, team_id: str, ids: set[str]) -> dict[str, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    rows = db.query(SlackUser).filter(SlackUser.team_id == team_id, SlackUser.user_id.in_(ids)).all()
    return {r.user_id: r.display_name for r in rows if r.display_name}


def get_or_create_conversation(db: Session, conn: SlackConnection, channel_id: str, conv_type: str | None = None) -> SlackConversation:
    conv = db.query(SlackConversation).filter_by(connection_id=conn.id, channel_id=channel_id).first()
    if conv:
        if not conv.is_active:
            conv.is_active = True  # an event proves the user is (again) a member
        return conv
    conv = SlackConversation(connection_id=conn.id, channel_id=channel_id,
                             conv_type=conv_type or conv_type_from_channel_type(None, channel_id))
    try:
        with db.begin_nested():
            db.add(conv)
            db.flush()
    except IntegrityError:
        conv = db.query(SlackConversation).filter_by(connection_id=conn.id, channel_id=channel_id).one()
    return conv


def _extract_body(raw: dict) -> str:
    text = raw.get("text") or ""
    if text.strip():
        return text
    files = raw.get("files") or []
    if files:
        return "[shared file: " + ", ".join((f.get("name") or f.get("title") or "file") for f in files[:3]) + "]"
    parts = [(a.get("fallback") or a.get("text") or a.get("title") or "") for a in (raw.get("attachments") or [])]
    return " ".join(p for p in parts if p).strip()


def _derive_artifacts(db: Session, row: SlackMessage) -> tuple[bool, bool]:
    """Reminder + meeting suggestion, one of each per message (auto_key), kept in sync on edits/deletes."""
    created = row.message_created_at or utcnow()
    now = utcnow()

    rem_key = f"auto:{row.id}"
    rem = db.query(Reminder).filter_by(auto_key=rem_key).first()
    due = None
    if not row.is_deleted and not row.is_self:  # your own requests to others are not your to-dos
        due = extract_reminder_due(row.text, now=created)
        if due and due < now - STALE_AFTER:
            due = None
    made_rem = made_ev = False
    if due:
        title = row.text[:280] or "Follow up from Slack"
        if rem is None:
            db.add(Reminder(owner_id=row.owner_id, message_id=row.id, title=title, due_at=due, auto_key=rem_key))
            made_rem = True
        elif rem.status == "pending":
            rem.title, rem.due_at = title, due
    elif rem is not None and rem.status == "pending":
        rem.status = "cancelled"

    ev = db.query(SuggestedEvent).filter_by(auto_key=rem_key).first()
    meeting = None if row.is_deleted else extract_meeting_suggestion(row.text, now=created)
    if meeting and meeting[1] < now - timedelta(hours=1) and created < now - timedelta(days=1):
        meeting = None
    if meeting:
        title, start, provisional = meeting
        duplicates = (db.query(SuggestedEvent).filter(
            SuggestedEvent.owner_id == row.owner_id, SuggestedEvent.status == "suggested",
            SuggestedEvent.title == title, SuggestedEvent.proposed_start == start)
            .order_by(SuggestedEvent.id).all())
        if ev is None and duplicates:
            ev = duplicates[0]
        for duplicate in duplicates:
            if ev is not None and duplicate.id != ev.id:
                duplicate.status = "cancelled"
        if ev is None:
            db.add(SuggestedEvent(owner_id=row.owner_id, message_id=row.id, title=title, proposed_start=start, duration_minutes=30,
                                  is_provisional=provisional, source_text=row.text, auto_key=rem_key))
            made_ev = True
        else:
            ev.status = "suggested"
            ev.title, ev.proposed_start, ev.is_provisional, ev.source_text = title, start, provisional, row.text
    elif ev is not None and ev.status == "suggested":
        ev.status = "cancelled"
    return made_rem, made_ev


def _refresh_parent_reply_count(db: Session, row: SlackMessage) -> None:
    if not row.thread_ts or row.thread_ts == row.slack_ts:
        return
    parent = db.query(SlackMessage).filter_by(connection_id=row.connection_id, channel_id=row.channel_id, slack_ts=row.thread_ts).first()
    if parent:
        n = db.query(func.count(SlackMessage.id)).filter(
            SlackMessage.connection_id == row.connection_id, SlackMessage.channel_id == row.channel_id,
            SlackMessage.thread_ts == row.thread_ts, SlackMessage.slack_ts != row.thread_ts,
            SlackMessage.is_deleted.is_(False)).scalar() or 0
        parent.reply_count = max(parent.reply_count or 0, n)


def ingest_message(db: Session, conn: SlackConnection, channel_id: str, raw: dict, *, conv_type: str | None = None,
                   source: str = "event", event_id: str | None = None) -> IngestResult:
    """Persist one Slack message for one connection. Idempotent: replays are `duplicate`, edits are `updated`."""
    ts, subtype = raw.get("ts"), raw.get("subtype")
    if not ts or not channel_id:
        return IngestResult("ignored", reason="missing channel/timestamp")
    if subtype not in ALLOWED_SUBTYPES:
        return IngestResult("ignored", reason=f"unsupported subtype {subtype}")
    is_bot = bool(raw.get("bot_id")) or subtype == "bot_message"
    if is_bot and not settings.ingest_bot_messages:
        return IngestResult("ignored", reason="bot messages disabled")
    raw_text = _extract_body(raw)[:8000]  # bound memory/embedding cost (Slack allows 40k chars)
    if not raw_text.strip():
        return IngestResult("ignored", reason="empty message")

    ts = str(ts)
    conv = get_or_create_conversation(db, conn, channel_id, conv_type)
    author = raw.get("user") or raw.get("bot_id")
    names = load_names(db, conn.team_id, mentioned_user_ids(raw_text) | {author})
    text = normalize_text(raw_text, names)
    author_name = (raw.get("username") or (raw.get("bot_profile") or {}).get("name")) if is_bot else names.get(author)
    is_self = bool(author) and author == conn.slack_user_id
    edited_ts = (raw.get("edited") or {}).get("ts")
    edited_at = ts_to_datetime(edited_ts) if edited_ts else None
    reply_count = int(raw.get("reply_count") or 0)
    thread_ts = raw.get("thread_ts")

    existing = db.query(SlackMessage).filter_by(connection_id=conn.id, channel_id=channel_id, slack_ts=ts).first()
    if existing is not None:
        if existing.is_deleted:
            return IngestResult("duplicate", existing, "message was deleted in Slack")
        if existing.edited_at and (edited_at is None or edited_at < existing.edited_at):
            return IngestResult("duplicate", existing, "stale edit (a newer version is already stored)")
        changed = existing.raw_text != raw_text or (edited_at and edited_at != existing.edited_at)
        if reply_count > (existing.reply_count or 0):
            existing.reply_count = reply_count
        if not changed:
            old_priority = (existing.priority_score, existing.priority_label, existing.reason)
            _rescore(existing, conn, conv, raw_text)
            _derive_artifacts(db, existing)
            if old_priority != (existing.priority_score, existing.priority_label, existing.reason):
                log_event(db, "score", "ok", f"priority refreshed: {existing.priority_label} ({existing.priority_score})",
                          owner_id=conn.owner_id, message_id=existing.id)
            if source == "event":
                log_event(db, "ingest", "duplicate", "already stored: replay dropped by unique identity", owner_id=conn.owner_id, message_id=existing.id)
            return IngestResult("duplicate", existing)
        existing.raw_text, existing.text, existing.edited_at = raw_text, text, edited_at
        existing.author_name = author_name or existing.author_name
        _rescore(existing, conn, conv, raw_text)
        _derive_artifacts(db, existing)
        assign_project(db, existing)
        log_event(db, "ingest", "ok", "edit applied; score and reminders re-derived", owner_id=conn.owner_id, message_id=existing.id)
        db.flush()
        return IngestResult("updated", existing)

    row = SlackMessage(
        owner_id=conn.owner_id, connection_id=conn.id, team_id=conn.team_id, channel_id=channel_id, conv_type=conv.conv_type,
        slack_ts=ts, thread_ts=thread_ts, reply_count=reply_count, user_id=author, author_name=author_name, text=text,
        raw_text=raw_text, is_self=is_self, is_bot=is_bot, source=source, source_event_id=event_id,
        message_created_at=ts_to_datetime(ts), edited_at=edited_at)
    _rescore(row, conn, conv, raw_text)
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:  # concurrent worker inserted the same message
        dup = db.query(SlackMessage).filter_by(connection_id=conn.id, channel_id=channel_id, slack_ts=ts).first()
        return IngestResult("duplicate", dup)
    made_rem, made_ev = _derive_artifacts(db, row)
    _refresh_parent_reply_count(db, row)
    assign_project(db, row)
    mid, oid = row.id, conn.owner_id
    log_event(db, "ingest", "ok", f"stored via {source} in {conv.name or channel_id} ({conv.conv_type})", owner_id=oid, message_id=mid)
    log_event(db, "score", "ok", f"{row.priority_label} ({row.priority_score}): {row.reason[:160]}", owner_id=oid, message_id=mid)
    if made_rem or made_ev:
        log_event(db, "extract", "ok", ", ".join(x for x, f in (("reminder", made_rem), ("meeting suggestion", made_ev)) if f) + " created", owner_id=oid, message_id=mid)
    db.flush()
    return IngestResult("created", row)


def _rescore(row: SlackMessage, conn: SlackConnection, conv: SlackConversation, raw_text: str) -> None:
    row.mentions_me = mentions_user(raw_text, conn.slack_user_id)
    score, label, reason = score_message(
        row.text, is_dm=(conv.conv_type == "im" and not row.is_self), mentions_me=row.mentions_me and not row.is_self,
        broadcast=has_broadcast(raw_text), is_bot=row.is_bot)
    row.priority_score, row.priority_label, row.reason = score, label, reason


def apply_deletion(db: Session, conn: SlackConnection, channel_id: str, ts: str) -> IngestResult:
    row = db.query(SlackMessage).filter_by(connection_id=conn.id, channel_id=channel_id, slack_ts=str(ts)).first()
    if row is None:
        return IngestResult("ignored", reason="deleted message was never stored")
    if row.is_deleted:
        return IngestResult("duplicate", row, "already deleted")
    row.is_deleted = True
    _derive_artifacts(db, row)  # cancels pending auto reminder/suggestion
    log_event(db, "ingest", "ok", "message deleted in Slack: tombstoned, reminders cancelled", owner_id=conn.owner_id, message_id=row.id)
    db.flush()
    return IngestResult("deleted", row)


def should_index(r: SlackMessage) -> bool:
    """Storage tiering: embed/graph only what is worth retrieving. P3 chatter outside any project stays in PostgreSQL only."""
    return (not r.is_deleted) and len(r.text or "") >= 15 and (r.priority_label != "P3" or r.project_id is not None)


def index_rows(db: Session, rows: list[SlackMessage]) -> dict:
    """Best-effort external indexing AFTER the source-of-truth commit. A Qdrant/Neo4j outage never loses a message."""
    live = [r for r in rows if should_index(r)]
    dead = [r.id for r in rows if r.is_deleted]
    results: dict = {}
    if rows and not live and not dead:
        log_event(db, "index_qdrant", "skipped", f"{len(rows)} low-value message(s) not embedded (tiering)", owner_id=rows[0].owner_id)
        db.commit()
    if live:
        results.update(integrations.index_messages(live))
        for key, stage in (("qdrant", "index_qdrant"), ("neo4j", "index_neo4j")):
            val = str(results.get(key, ""))
            log_event(db, stage, "error" if val.startswith("error") else "skipped" if val.startswith("skipped") else "ok",
                      f"{len(live)} message(s): {val[:200]}", owner_id=live[0].owner_id)
        db.commit()
        if results.get("vector_ok"):
            now = utcnow()
            for r in live:
                r.indexed_at = now
            db.commit()
    if dead:
        results["deleted"] = integrations.delete_message_index(dead)
    for key in ("qdrant", "neo4j"):
        if str(results.get(key, "")).startswith("error"):
            log.warning("index_failed target=%s detail=%s", key, results[key])
    return results


def purge_connection_data(db: Session, conn: SlackConnection) -> int:
    """Delete everything imported through a connection (messages, derived reminders/suggestions, conversations, index entries)."""
    ids = [i for (i,) in db.query(SlackMessage.id).filter(SlackMessage.connection_id == conn.id).all()]
    if ids:
        db.query(Reminder).filter(Reminder.message_id.in_(ids)).delete(synchronize_session=False)
        db.query(SuggestedEvent).filter(SuggestedEvent.message_id.in_(ids)).delete(synchronize_session=False)
        db.query(OutboundAction).filter(OutboundAction.message_id.in_(ids)).update({"message_id": None}, synchronize_session=False)
        db.query(SlackMessage).filter(SlackMessage.id.in_(ids)).delete(synchronize_session=False)
        integrations.delete_message_index(ids)
    db.query(SlackConversation).filter(SlackConversation.connection_id == conn.id).delete(synchronize_session=False)
    return len(ids)
