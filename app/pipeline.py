"""The single ingestion path. Live events, history sync, reconcile and the demo all go through ingest_message(),
so a message is scored, de-duplicated and turned into reminders/suggestions exactly one way."""
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import integrations, pipeline_events, projects
from app.config import settings
from app.models import (MessageFeedback, MessageProject, MessageState, OutboundAction, Reminder, SlackConnection, SlackConversation, SlackMessage, SlackUser,
                        SuggestedEvent, utcnow)
from app import ranker
from app.priority import extract_meeting_suggestion, extract_reminder_due
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


def _is_directed(row: SlackMessage) -> bool:
    """True when the message is addressed to the connected user (not a bot, not channel chatter)."""
    if row.is_bot:
        return False
    return bool(row.mentions_me) or row.conv_type in ("im", "mpim")


def _derive_artifacts(db: Session, row: SlackMessage) -> None:
    """Reminder + meeting suggestion, one of each per message (auto_key), kept in sync on edits/deletes."""
    created = row.message_created_at or utcnow()
    now = utcnow()

    rem_key = f"auto:{row.id}"
    rem = db.query(Reminder).filter_by(auto_key=rem_key).first()
    # Only things aimed at YOU become to-dos or meeting ideas: an @mention, a DM, or an email written to you. A channel
    # message, a bot alert or a newsletter that happens to contain "today" or "call" is somebody else's business, and
    # turning each of them into a reminder is what flooded the inbox.
    directed = _is_directed(row)
    due = None
    if directed and not row.is_deleted and not row.is_self:  # your own requests to others are not your to-dos
        due = extract_reminder_due(row.text, now=created)
        if due and due < now - STALE_AFTER:
            due = None
    if due:
        title = row.text[:280] or "Follow up from Slack"
        if rem is None:
            db.add(Reminder(owner_id=row.owner_id, message_id=row.id, title=title, due_at=due, auto_key=rem_key))
        elif rem.status == "pending":
            rem.title, rem.due_at = title, due
    elif rem is not None and rem.status == "pending":
        rem.status = "cancelled"

    ev = db.query(SuggestedEvent).filter_by(auto_key=rem_key).first()
    meeting = None if (row.is_deleted or not directed) else extract_meeting_suggestion(row.text, now=created)
    if meeting and meeting[1] < now - timedelta(hours=1):
        meeting = None
    if meeting:
        title, start, provisional = meeting
        if ev is None:
            db.add(SuggestedEvent(owner_id=row.owner_id, message_id=row.id, title=title, proposed_start=start, duration_minutes=30,
                                  is_provisional=provisional, source_text=row.text, auto_key=rem_key))
        elif ev.status == "suggested":
            ev.title, ev.proposed_start, ev.is_provisional, ev.source_text = title, start, provisional, row.text
    elif ev is not None and ev.status == "suggested":
        ev.status = "cancelled"


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
    raw_text = _extract_body(raw)
    if not raw_text.strip():
        return IngestResult("ignored", reason="empty message")

    ts = str(ts)
    conv = get_or_create_conversation(db, conn, channel_id, conv_type)
    author = raw.get("user") or raw.get("bot_id")
    names = load_names(db, conn.team_id, mentioned_user_ids(raw_text) | {author})
    text = normalize_text(raw_text, names)
    author_name = (raw.get("username") or (raw.get("bot_profile") or {}).get("name")) if is_bot else (names.get(author) or raw.get("_name"))
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
        if existing.compacted_at is not None:
            # retention dropped the raw text, so "text differs" proves nothing: only a genuinely newer edit counts
            changed = bool(edited_at and (existing.edited_at is None or edited_at > existing.edited_at))
        else:
            changed = existing.raw_text != raw_text or bool(edited_at and edited_at != existing.edited_at)
        if reply_count > (existing.reply_count or 0):
            existing.reply_count = reply_count
        if not changed:
            return IngestResult("duplicate", existing)
        existing.raw_text, existing.text, existing.edited_at, existing.compacted_at = raw_text, text, edited_at, None
        existing.author_name = author_name or existing.author_name
        _rescore(existing, conn, conv, raw_text, bool(raw.get("_direct")), db)
        _derive_artifacts(db, existing)
        db.flush()
        return IngestResult("updated", existing)

    row = SlackMessage(
        owner_id=conn.owner_id, connection_id=conn.id, team_id=conn.team_id, channel_id=channel_id, conv_type=conv.conv_type,
        slack_ts=ts, thread_ts=thread_ts, reply_count=reply_count, user_id=author, author_name=author_name, text=text,
        raw_text=raw_text, is_self=is_self, is_bot=is_bot, source=source, source_event_id=event_id,
        message_created_at=ts_to_datetime(ts), edited_at=edited_at)
    _rescore(row, conn, conv, raw_text, bool(raw.get("_direct")), db)
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:  # concurrent worker inserted the same message
        dup = db.query(SlackMessage).filter_by(connection_id=conn.id, channel_id=channel_id, slack_ts=ts).first()
        return IngestResult("duplicate", dup)
    _derive_artifacts(db, row)
    _refresh_parent_reply_count(db, row)
    db.flush()
    return IngestResult("created", row)


def _rescore(row: SlackMessage, conn: SlackConnection, conv: SlackConversation, raw_text: str, direct: bool = False, db=None) -> None:
    if conv.conv_type == "email":  # an email written to me personally (not a list/newsletter) counts like an @mention
        row.mentions_me = direct and not row.is_self
        _apply_rank(row, db, is_dm=False, mentions_me=row.mentions_me, is_bot=row.is_bot)
        return
    row.mentions_me = mentions_user(raw_text, conn.slack_user_id)
    _apply_rank(row, db, is_dm=(conv.conv_type == "im" and not row.is_self), mentions_me=row.mentions_me and not row.is_self, is_bot=row.is_bot)


def _apply_rank(row: SlackMessage, db, *, is_dm: bool, mentions_me: bool, is_bot: bool) -> None:
    shifts = ranker.sender_shifts(db, row.owner_id) if db is not None else {}
    row.priority_score, row.priority_label, row.reason = ranker.rank_message(
        row.text, row.message_created_at, row.author_name, is_dm=is_dm, mentions_me=mentions_me, is_bot=is_bot,
        sender_shift=shifts.get(ranker.sender_key(row), 0.0))


def apply_deletion(db: Session, conn: SlackConnection, channel_id: str, ts: str) -> IngestResult:
    row = db.query(SlackMessage).filter_by(connection_id=conn.id, channel_id=channel_id, slack_ts=str(ts)).first()
    if row is None:
        return IngestResult("ignored", reason="deleted message was never stored")
    if row.is_deleted:
        return IngestResult("duplicate", row, "already deleted")
    row.is_deleted = True
    _derive_artifacts(db, row)  # cancels pending auto reminder/suggestion
    db.flush()
    return IngestResult("deleted", row)


def index_rows(db: Session, rows: list[SlackMessage], trace_id: str | None = None) -> dict:
    """Best-effort external indexing AFTER the source-of-truth commit. A Qdrant/Neo4j outage never loses a message.

    Tiering: only P0-P2 are embedded / graphed. P3 chatter ("lunch?") stays in PostgreSQL only, and if a message was
    indexed and later drops to P3 (an edit) its index entries are removed."""
    rows = [r for r in rows if r is not None]
    if not rows:
        return {}
    live = [r for r in rows if not r.is_deleted and r.priority_label != "P3"]
    dead = [r for r in rows if r.is_deleted or r.priority_label == "P3"]
    dead_ids = [r.id for r in dead if r.is_deleted or r.indexed_at is not None]
    results: dict = {}
    owner_id = rows[0].owner_id
    if live:
        results.update(integrations.index_messages(live))
        if results.get("vector_ok"):
            now = utcnow()
            for r in live:
                r.indexed_at = now
    if dead_ids:
        results["deleted"] = integrations.delete_message_index(dead_ids)
        for r in dead:
            r.indexed_at = None
    for key, stage in (("qdrant", "indexed_vector"), ("neo4j", "indexed_graph")):
        val = str(results.get(key, ""))
        if val.startswith("error"):
            log.warning("index_failed target=%s detail=%s", key, val[:200])
        if live or val:
            status = "error" if val.startswith("error") else "skipped" if (not val or val.startswith("skipped")) else "ok"
            pipeline_events.record(db, stage, status, owner_id=owner_id, trace_id=trace_id,
                                   message_id=live[0].id if len(live) == 1 else None,
                                   detail=(f"{len(live)} message(s): {val}" if val else f"{len(live)} message(s) not indexed")[:300])
    skipped_p3 = sum(1 for r in dead if not r.is_deleted)
    if skipped_p3:
        pipeline_events.record(db, "indexed_vector", "skipped", owner_id=owner_id, trace_id=trace_id,
                               detail=f"{skipped_p3} low-priority (P3) message(s) kept in PostgreSQL only")
    db.commit()
    return results


def log_batch(db: Session, conn: SlackConnection, rows: list[SlackMessage], *, fetched: int, trace_id: str | None, label: str) -> None:
    """One summary row per stage for a whole history page (a per-message row for 5000 messages would drown the page)."""
    rows = [r for r in rows if r is not None]
    if not fetched and not rows:
        return
    pipeline_events.record(db, "sync_page", "ok", owner_id=conn.owner_id, team_id=conn.team_id, trace_id=trace_id,
                           detail=f"{label}: {fetched} fetched, {len(rows)} new/changed")
    if not rows:
        return
    pr = Counter(r.priority_label for r in rows)
    ids = [r.id for r in rows if r.id is not None]
    rem = db.query(func.count(Reminder.id)).filter(Reminder.message_id.in_(ids)).scalar() or 0 if ids else 0
    mt = db.query(func.count(SuggestedEvent.id)).filter(SuggestedEvent.message_id.in_(ids)).scalar() or 0 if ids else 0
    pipeline_events.record(db, "ingested", "ok", owner_id=conn.owner_id, trace_id=trace_id, detail=f"{label}: {len(rows)} normalised + de-duplicated")
    pipeline_events.record(db, "scored", "ok", owner_id=conn.owner_id, trace_id=trace_id,
                           detail=f"{label}: " + " ".join(f"{k}={pr.get(k, 0)}" for k in ("P0", "P1", "P2", "P3")))
    pipeline_events.record(db, "extracted", "ok" if (rem or mt) else "skipped", owner_id=conn.owner_id, trace_id=trace_id,
                           detail=f"{label}: {rem} reminder(s), {mt} meeting suggestion(s)")


def link_and_log(db: Session, rows: list[SlackMessage], *, trace_id: str | None = None) -> None:
    """Attach messages to projects AFTER the source-of-truth commit; a failure here never loses a message."""
    live = [r for r in rows if r is not None and r.id is not None]
    if not live:
        return
    try:
        linked = projects.link_rows(db, live)
        n = sum(1 for v in linked.values() if v)
        if n:
            pipeline_events.record(db, "project_linked", "ok", owner_id=live[0].owner_id, trace_id=trace_id,
                                   message_id=live[0].id if len(live) == 1 else None,
                                   detail=f"{n} of {len(live)} message(s) linked to existing projects")
        db.commit()
    except Exception:
        db.rollback()
        log.exception("project_link_failed")


def purge_connection_data(db: Session, conn: SlackConnection) -> int:
    """Delete everything imported through a connection (messages, derived reminders/suggestions, conversations, index entries)."""
    ids = [i for (i,) in db.query(SlackMessage.id).filter(SlackMessage.connection_id == conn.id).all()]
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        db.query(MessageProject).filter(MessageProject.message_id.in_(chunk)).delete(synchronize_session=False)
        db.query(MessageState).filter(MessageState.message_id.in_(chunk)).delete(synchronize_session=False)
        db.query(MessageFeedback).filter(MessageFeedback.message_id.in_(chunk)).delete(synchronize_session=False)
        db.query(Reminder).filter(Reminder.message_id.in_(chunk)).delete(synchronize_session=False)
        db.query(SuggestedEvent).filter(SuggestedEvent.message_id.in_(chunk)).delete(synchronize_session=False)
        db.query(OutboundAction).filter(OutboundAction.message_id.in_(chunk)).update({"message_id": None}, synchronize_session=False)
        db.query(SlackMessage).filter(SlackMessage.id.in_(chunk)).delete(synchronize_session=False)
    if ids:
        integrations.delete_message_index(ids)
        projects.refresh_stats(db, owner_id=conn.owner_id)
    db.query(SlackConversation).filter(SlackConversation.connection_id == conn.id).delete(synchronize_session=False)
    return len(ids)
