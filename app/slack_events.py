"""Live Slack events: durable acceptance (HTTP side) and fan-out/processing (worker side).

Slack delivers ONE event per workspace message; ContextOS stores a personal copy for every connected user who
is a member of that conversation. Per-connection work is a separate job (slack_process_message) so one user's
failure never blocks or repeats another's.
"""
import json
import logging
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.cache import invalidate_user_caches
from app.jobs import SLACK_EVENT, schedule_discovery
from app.trace import log_event
from app.models import EventReceipt, ProcessingJob, SlackConnection, SlackConversation, utcnow
from app.pipeline import apply_deletion, index_rows, ingest_message
from app.slack_client import SlackClient
from app.slack_oauth import get_token, wipe_tokens
from app.slack_sync import ensure_users, enqueue_message
from app.slack_text import conv_type_from_channel_type

log = logging.getLogger("contextos.events")


def accept_event(db: Session, payload: dict[str, Any]) -> dict:
    """Store the event receipt + a durable job in ONE transaction. Used by the HTTP endpoint and by the demo."""
    event_id = payload.get("event_id")
    if not event_id:
        return {"ok": False, "error": "missing event_id"}
    event = payload.get("event") or {}
    team_id = payload.get("team_id") or (payload.get("team") or {}).get("id")
    etype = str(event.get("type") or payload.get("type") or "unknown")
    try:
        db.add(EventReceipt(event_id=event_id, team_id=team_id, event_type=etype))
        db.add(ProcessingJob(job_type=SLACK_EVENT, payload_json=json.dumps(payload), status="queued", team_id=team_id))
        log_event(db, "event_received", "ok", f"{etype} accepted: receipt + job in one transaction", team_id=team_id)
        db.commit()
    except IntegrityError:
        db.rollback()
        log_event(db, "event_received", "duplicate", f"{etype} redelivery dropped (event_id already received)", team_id=team_id)
        db.commit()
        return {"ok": True, "duplicate": True}
    return {"ok": True, "queued": True, "event_id": event_id}


def _target_connections(db: Session, team_id: str, channel_id: str, payload: dict) -> list[SlackConnection]:
    conns = db.query(SlackConnection).filter(SlackConnection.team_id == team_id, SlackConnection.status == "connected").all()
    if not conns:
        return []
    member_ids = {c.connection_id for c in db.query(SlackConversation).filter(
        SlackConversation.channel_id == channel_id, SlackConversation.is_active.is_(True),
        SlackConversation.connection_id.in_([c.id for c in conns])).all()}
    authorized = {a.get("user_id") for a in (payload.get("authorizations") or []) if not a.get("is_bot")}
    return [c for c in conns if c.id in member_ids or c.slack_user_id in authorized]


def _handle_revocation(db: Session, team_id: str, event: dict) -> dict:
    etype = event.get("type")
    q = db.query(SlackConnection).filter(SlackConnection.team_id == team_id)
    if etype == "tokens_revoked":
        users = set((event.get("tokens") or {}).get("oauth") or [])
        conns = [c for c in q.all() if c.slack_user_id in users]
    else:  # app_uninstalled
        conns = q.all()
    for c in conns:
        c.status, c.revoked_at, c.sync_state = "revoked", utcnow(), "idle"
        c.last_sync_error = f"Slack reported {etype}; reconnect to resume syncing."
        wipe_tokens(c)
    db.commit()
    for c in conns:
        invalidate_user_caches(c.owner_id)
    return {"revoked_connections": len(conns)}


def process_slack_event(db: Session, payload: dict[str, Any]) -> dict:
    """Worker side of a `slack_event` job: decide which connections get this message and enqueue per-connection jobs."""
    event = payload.get("event") or {}
    etype = event.get("type") or payload.get("type")
    team_id = payload.get("team_id") or (payload.get("team") or {}).get("id")
    if etype in ("tokens_revoked", "app_uninstalled"):
        return _handle_revocation(db, team_id, event)
    if etype != "message" or not team_id:
        return {"ignored": True, "reason": f"unsupported event type {etype}"}

    subtype = event.get("subtype")
    channel_id, channel_type = event.get("channel"), event.get("channel_type")
    if subtype == "message_changed":
        message, op, ts = dict(event.get("message") or {}), "upsert", (event.get("message") or {}).get("ts")
        message.setdefault("channel", channel_id)
    elif subtype == "message_deleted":
        message, op, ts = None, "delete", event.get("deleted_ts")
    else:
        message, op, ts = event, "upsert", event.get("ts")
    if not channel_id or not ts:
        return {"ignored": True, "reason": "message missing channel/timestamp"}

    targets = _target_connections(db, team_id, channel_id, payload)
    queued = 0
    for conn in targets:
        job = enqueue_message(db, conn, channel_id, op, message=message, ts=ts, channel_type=channel_type, event_id=payload.get("event_id"))
        queued += job is not None
        conn.last_event_at = utcnow()
        log_event(db, "fanout", "ok", f"{op} for channel {channel_id} routed to this user's connection", owner_id=conn.owner_id)
    if not targets:
        log_event(db, "fanout", "skipped", f"no connected member of {channel_id}", team_id=team_id)
    db.commit()
    return {"fanout": len(targets), "queued": queued, "op": op}


def process_message_job(db: Session, payload: dict[str, Any]) -> dict:
    """Worker side of `slack_process_message`: ingest/update/delete one message for one connection."""
    conn = db.query(SlackConnection).filter_by(id=payload.get("connection_id")).first()
    if conn is None or conn.status != "connected":
        return {"ignored": True, "reason": "connection missing or not active"}
    channel_id, op = payload["channel_id"], payload.get("op", "upsert")
    ctype = conv_type_from_channel_type(payload.get("channel_type"), channel_id)
    if op == "delete":
        res = apply_deletion(db, conn, channel_id, payload["ts"])
    else:
        message = payload.get("message") or {}
        author = message.get("user")
        if author and not conn.is_demo:  # best-effort name resolution; never blocks ingestion
            try:
                with SlackClient(get_token(db, conn), max_inline_wait=2, retries=1) as client:
                    ensure_users(db, client, conn.team_id, {author})
            except Exception as exc:
                log.info("name_lookup_skipped conn=%s reason=%s", conn.id, type(exc).__name__)
        res = ingest_message(db, conn, channel_id, message, conv_type=ctype,
                             source="demo" if conn.is_demo else "event", event_id=payload.get("event_id"))
    db.commit()
    integrations = index_rows(db, [res.message]) if res.message is not None and res.action in ("created", "updated", "deleted") else {}
    if res.action != "duplicate" and res.action != "ignored":
        invalidate_user_caches(conn.owner_id)
        schedule_discovery(db, conn.owner_id)
        db.commit()
    out = {"action": res.action, "reason": res.reason}
    if res.message is not None:
        out.update({"message_id": res.message.id, "priority": res.message.priority_label, "score": res.message.priority_score})
    if integrations:
        out["integrations"] = integrations
    return out
