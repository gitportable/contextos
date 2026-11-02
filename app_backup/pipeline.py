"""The single ingestion path. Live events, history sync, reconcile and the demo all go through ingest_message(),
so messages are normalized, structurally scored, and de-duplicated consistently."""
import json
import logging
from collections import Counter
from dataclasses import dataclass
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import integrations, pipeline_events, projects
from app.config import settings
from app.item_sources import detach_message_sources
from app.models import (Item, ItemEvent, ItemSource, MessageProject, OutboundAction, Reminder, SlackConnection,
                        SlackConversation, SlackMessage, SlackUser, SuggestedEvent, utcnow)
from app.priority import score_message
from app.slack_text import (conv_type_from_channel_type, has_broadcast, mentioned_user_ids, mentions_user,
                            normalize_text, ts_to_datetime)

log = logging.getLogger("contextos.pipeline")
# Slack subtypes that carry real human/bot content. Joins, topic changes, etc. are ignored.
ALLOWED_SUBTYPES = {None, "thread_broadcast", "file_share", "me_message", "bot_message"}


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


def _derive_artifacts(db: Session, row: SlackMessage) -> None:
    """Retire legacy regex-generated artifacts; message text alone is not confirmation."""
    rem_key = f"auto:{row.id}"
    rem = db.query(Reminder).filter_by(auto_key=rem_key).first()
    if rem is not None and rem.status == "pending":
        rem.status = "cancelled"

    ev = db.query(SuggestedEvent).filter_by(auto_key=rem_key).first()
    if ev is not None and ev.status == "suggested":
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
        _rescore(existing, conn, conv, raw_text, bool(raw.get("_direct")), has_attachment=bool(raw.get("files") or raw.get("attachments")))
        _derive_artifacts(db, existing)
        db.flush()
        return IngestResult("updated", existing)

    row = SlackMessage(
        owner_id=conn.owner_id, connection_id=conn.id, team_id=conn.team_id, channel_id=channel_id, conv_type=conv.conv_type,
        slack_ts=ts, thread_ts=thread_ts, reply_count=reply_count, user_id=author, author_name=author_name, text=text,
        raw_text=raw_text, is_self=is_self, is_bot=is_bot, source=source, source_event_id=event_id,
        message_created_at=ts_to_datetime(ts), edited_at=edited_at)
    _rescore(row, conn, conv, raw_text, bool(raw.get("_direct")), has_attachment=bool(raw.get("files") or raw.get("attachments")))
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


def _rescore(row: SlackMessage, conn: SlackConnection, conv: SlackConversation, raw_text: str, direct: bool = False,
             *, has_attachment: bool = False) -> None:
    if conv.conv_type == "email":  # an email written to me personally (not a list/newsletter) counts like an @mention
        row.mentions_me = direct and not row.is_self
        score, label, reason = score_message(
            is_dm=False, mentions_me=row.mentions_me, broadcast=False, is_bot=row.is_bot, is_self=row.is_self,
            reply_count=row.reply_count, has_attachment=has_attachment)
        row.priority_score, row.priority_label, row.reason = score, label, reason
        return
    row.mentions_me = mentions_user(raw_text, conn.slack_user_id)
    score, label, reason = score_message(
        is_dm=(conv.conv_type == "im" and not row.is_self), mentions_me=row.mentions_me and not row.is_self,
        broadcast=has_broadcast(raw_text), is_bot=row.is_bot, is_self=row.is_self,
        reply_count=row.reply_count, has_attachment=has_attachment)
    row.priority_score, row.priority_label, row.reason = score, label, reason


def apply_deletion(db: Session, conn: SlackConnection, channel_id: str, ts: str) -> IngestResult:
    row = db.query(SlackMessage).filter_by(connection_id=conn.id, channel_id=channel_id, slack_ts=str(ts)).first()
    if row is None:
        return IngestResult("ignored", reason="deleted message was never stored")
    if row.is_deleted:
        return IngestResult("duplicate", row, "already deleted")
    row.is_deleted = True
    detach_message_sources(db, [row.id])
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
                           detail=f"{label}: {rem} legacy/manual reminder(s), {mt} legacy/manual meeting suggestion(s)")


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
    """Delete a connection's messages, derived records, items, feedback events, conversations, and index entries."""
    ids = [i for (i,) in db.query(SlackMessage.id).filter(SlackMessage.connection_id == conn.id).all()]
    item_ids = {i for (i,) in db.query(Item.id).filter(Item.connection_id == conn.id).all()}
    item_ids.update(i for (i,) in db.query(ItemSource.item_id).filter(ItemSource.connection_id == conn.id).distinct().all())
    if item_ids:
        detach_message_sources(db, ids)
        for item_id in item_ids:
            remaining = db.query(ItemSource).filter_by(item_id=item_id).all()
            if remaining:
                item = db.query(Item).filter_by(id=item_id).first()
                if item:
                    remaining_ids = sorted(source.message_id for source in remaining)
                    item.source_message_ids = json.dumps(remaining_ids, separators=(",", ":"))
                    remaining_connections = {source.connection_id for source in remaining}
                    item.connection_id = next(iter(remaining_connections)) if len(remaining_connections) == 1 else None
            else:
                db.query(ItemEvent).filter(ItemEvent.item_id == item_id).delete(synchronize_session=False)
                db.query(Item).filter(Item.id == item_id).delete(synchronize_session=False)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        db.query(MessageProject).filter(MessageProject.message_id.in_(chunk)).delete(synchronize_session=False)
        db.query(Reminder).filter(Reminder.message_id.in_(chunk)).delete(synchronize_session=False)
        db.query(SuggestedEvent).filter(SuggestedEvent.message_id.in_(chunk)).delete(synchronize_session=False)
        db.query(ItemEvent).filter(ItemEvent.message_id.in_(chunk)).delete(synchronize_session=False)
        db.query(OutboundAction).filter(OutboundAction.message_id.in_(chunk)).update({"message_id": None}, synchronize_session=False)
        db.query(SlackMessage).filter(SlackMessage.id.in_(chunk)).delete(synchronize_session=False)
    if ids:
        integrations.delete_message_index(ids)
        projects.refresh_stats(db, owner_id=conn.owner_id)
    db.query(SlackConversation).filter(SlackConversation.connection_id == conn.id).delete(synchronize_session=False)
    return len(ids)
