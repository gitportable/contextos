"""Historical + incremental Slack synchronisation for ONE connection.

Guarantees
  * cursor pagination everywhere (users.conversations cursor; history via `latest` boundary so it is resumable)
  * every page and its checkpoint are committed atomically -> a crash/429 resumes where it stopped
  * Retry-After is honoured: short waits inline, long waits reschedule the job (RescheduleJob)
  * threads (conversations.replies) are fetched, edits are applied, deletions are detected by reconcile
  * only conversation types whose scopes were actually granted are synced
"""
import html
import logging
from datetime import timedelta
from typing import Callable

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import settings
from app.crypto import TokenCryptoError
from app.jobs import (schedule_discovery, PermanentJobError, RescheduleJob, SLACK_INCREMENTAL_SYNC, SLACK_INITIAL_SYNC, SLACK_PROCESS_MESSAGE,
                      SLACK_RECONCILE, enqueue_job)
from app.models import (OutboundAction, SlackConnection, SlackConversation, SlackMessage, SlackUser, as_utc, utcnow)
from app.pipeline import apply_deletion, get_or_create_conversation, index_rows, ingest_message
from app.slack_client import (SlackApiError, SlackAuthError, SlackChannelError, SlackClient, SlackRateLimited, SlackScopeError)
from app.slack_oauth import get_token, mark_needs_reauth
from app.trace import log_event
from app.slack_text import (datetime_to_ts, mentioned_user_ids, normalize_text, ts_max, ts_min, ts_to_datetime, ts_to_decimal)

log = logging.getLogger("contextos.sync")

READ_SCOPE = {"public_channel": "channels:read", "private_channel": "groups:read", "im": "im:read", "mpim": "mpim:read"}
HISTORY_SCOPE = {"public_channel": "channels:history", "private_channel": "groups:history", "im": "im:history", "mpim": "mpim:history"}
TYPE_ORDER = {"im": 0, "mpim": 1, "private_channel": 2, "public_channel": 3}
MAX_THREADS_PER_RUN = 50


def syncable_types(conn: SlackConnection) -> list[str]:
    """Conversation types for which BOTH the discovery and the history scope were granted."""
    return [t for t in TYPE_ORDER if conn.has_scope(READ_SCOPE[t]) and conn.has_scope(HISTORY_SCOPE[t])]


def missing_scopes(conn: SlackConnection) -> list[str]:
    requested = {x for x in (conn.requested_user_scopes or "").split(",") if x}
    return sorted(requested - conn.scopes("user"))


# ------------------------------------------------------------------ queue ---
def enqueue_sync(db: Session, conn: SlackConnection, mode: str = "auto", channel_id: str | None = None):
    """Queue at most one sync job per connection at a time. Returns the job or None if one is already pending."""
    if conn.status != "connected":
        return None
    if mode == "auto":
        mode = "initial" if not db.query(SlackConversation).filter_by(connection_id=conn.id, backfill_complete=True).first() else "incremental"
    job_type = {"initial": SLACK_INITIAL_SYNC, "incremental": SLACK_INCREMENTAL_SYNC, "reconcile": SLACK_RECONCILE}[mode]
    job = enqueue_job(db, job_type, {"connection_id": conn.id, "channel_id": channel_id}, connection_id=conn.id,
                      dedupe_key=f"sync:{conn.id}" if mode != "reconcile" else f"reconcile:{conn.id}:{channel_id or 'all'}")
    if job is not None and mode != "reconcile":
        conn.sync_state = "queued"
    return job


def enqueue_message(db: Session, conn: SlackConnection, channel_id: str, op: str, *, message: dict | None = None,
                    ts: str | None = None, channel_type: str | None = None, event_id: str | None = None):
    """Create a slack_process_message job (op = upsert | delete). Duplicate in-flight jobs are dropped."""
    mts = ts or (message or {}).get("ts")
    edit = ((message or {}).get("edited") or {}).get("ts") or ""
    payload = {"connection_id": conn.id, "channel_id": channel_id, "op": op, "message": message, "ts": mts,
               "channel_type": channel_type, "event_id": event_id}
    return enqueue_job(db, SLACK_PROCESS_MESSAGE, payload, connection_id=conn.id,
                       dedupe_key=f"msg:{conn.id}:{channel_id}:{mts}:{op}:{edit}")


# ------------------------------------------------------------------ users ---
def ensure_users(db: Session, client: SlackClient, team_id: str, user_ids: set[str | None]) -> None:
    """Cache display names (needs users:read). Never fails the sync: without the scope we simply show IDs."""
    ids = {i for i in user_ids if i and i[0] in "UW"}
    if not ids:
        return
    cached = {u.user_id: u for u in db.query(SlackUser).filter(SlackUser.team_id == team_id, SlackUser.user_id.in_(ids)).all()}
    stale = utcnow() - timedelta(days=7)
    todo = [i for i in ids if i not in cached or as_utc(cached[i].updated_at) < stale][:100]
    for uid in todo:
        display, username, email, is_bot = uid, "", None, False
        try:
            user = client.call("users.info", {"user": uid}).get("user") or {}
            profile = user.get("profile") or {}
            display = profile.get("display_name") or user.get("real_name") or profile.get("real_name") or user.get("name") or uid
            username = user.get("name") or ""
            email = profile.get("email")
            is_bot = bool(user.get("is_bot"))
        except (SlackScopeError, SlackRateLimited, SlackAuthError):
            break
        except SlackApiError as exc:
            if exc.code != "user_not_found":
                continue
        row = cached.get(uid)
        if row is None:
            db.add(SlackUser(team_id=team_id, user_id=uid, display_name=display, username=username, email=email, is_bot=is_bot))
        else:
            row.display_name, row.username, row.email, row.is_bot, row.updated_at = display, username, email, is_bot, utcnow()
    db.commit()


# -------------------------------------------------------------- discovery ---
def discover_conversations(db: Session, client: SlackClient, conn: SlackConnection) -> list[SlackConversation]:
    """List the conversations the user belongs to (cursor-paginated) and upsert them."""
    types = syncable_types(conn)
    if not types:
        raise SlackScopeError("users.conversations", "missing_scope", {"needed": "channels:history,channels:read (or groups/im/mpim equivalents)"})
    existing = {c.channel_id: c for c in db.query(SlackConversation).filter_by(connection_id=conn.id).all()}
    seen: set[str] = set()
    peers: dict[str, SlackConversation] = {}
    for page in client.paginate("users.conversations", {"types": ",".join(types), "exclude_archived": "true", "limit": 200}):
        for ch in page.get("channels", []):
            cid = ch.get("id")
            if not cid:
                continue
            ctype = "im" if ch.get("is_im") else "mpim" if ch.get("is_mpim") else "private_channel" if (ch.get("is_private") or ch.get("is_group")) else "public_channel"
            conv = existing.get(cid) or get_or_create_conversation(db, conn, cid, ctype)
            existing[cid] = conv
            conv.conv_type, conv.is_active, conv.is_archived = ctype, True, bool(ch.get("is_archived"))
            conv.peer_user_id = ch.get("user") if ctype == "im" else None
            if ch.get("name"):
                conv.name = ch["name"]
            if ctype == "im" and conv.peer_user_id:
                peers[conv.peer_user_id] = conv
            seen.add(cid)
    for cid, conv in existing.items():  # user left (or was removed from) these
        if cid not in seen and conv.conv_type in types and conv.is_active:
            conv.is_active = False
    db.commit()
    if peers:
        ensure_users(db, client, conn.team_id, set(peers))
        names = {u.user_id: u.display_name for u in db.query(SlackUser).filter(SlackUser.team_id == conn.team_id, SlackUser.user_id.in_(set(peers))).all()}
        for uid, conv in peers.items():
            conv.name = "DM: " + names.get(uid, uid)
        db.commit()
    active = [c for c in existing.values() if c.is_active]
    return sorted(active, key=lambda c: (TYPE_ORDER.get(c.conv_type, 9), c.channel_id))


# ---------------------------------------------------------------- history ---
def fetch_conversation_history(client: SlackClient, channel_id: str, *, oldest: str | None = None, latest: str | None = None,
                               page_size: int | None = None):
    """Yield pages (newest->oldest). Pages are chained with the `latest` boundary, not cursors, so a resumed run can
    restart from a persisted ts. Slack's bounds are exclusive by default, so no message is fetched twice."""
    size = max(1, min(page_size or settings.slack_history_page_size, 1000))
    while True:
        params = {"channel": channel_id, "limit": size}
        if oldest:
            params["oldest"] = oldest
        if latest:
            params["latest"] = latest
        data = client.call("conversations.history", params)
        msgs = data.get("messages") or []
        yield msgs
        if not data.get("has_more") or not msgs:
            return
        latest = ts_min(*[m.get("ts") for m in msgs])


def fetch_thread_replies(client: SlackClient, channel_id: str, thread_ts: str):
    """All replies of a thread (cursor-paginated), excluding the parent that Slack repeats as the first item."""
    params = {"channel": channel_id, "ts": thread_ts, "limit": max(1, min(settings.slack_history_page_size, 1000))}
    for page in client.paginate("conversations.replies", params):
        for m in page.get("messages") or []:
            if m.get("ts") != thread_ts:
                yield m


def _ensure_authors(db, client, conn, msgs):
    ids: set[str | None] = set()
    for m in msgs:
        ids.add(m.get("user"))
        ids |= mentioned_user_ids(m.get("text") or "")
    ensure_users(db, client, conn.team_id, ids)


def ingest_thread(db, client, conn, conv, parent_ts, source="history"):
    try:
        replies = list(fetch_thread_replies(client, conv.channel_id, parent_ts))
    except SlackChannelError:  # parent deleted or thread gone
        return [], 0
    _ensure_authors(db, client, conn, replies)
    rows, created = [], 0
    for m in sorted(replies, key=lambda m: ts_to_decimal(m.get("ts"))):
        res = ingest_message(db, conn, conv.channel_id, m, conv_type=conv.conv_type, source=source)
        if res.action in ("created", "updated"):
            rows.append(res.message)
        created += res.action == "created"
    return rows, created


def process_page(db, client, conn, conv, msgs, *, fetch_threads=True, source="history"):
    """Ingest one history page (+ the threads of its parents). Does not commit. Returns (rows, created_count)."""
    _ensure_authors(db, client, conn, msgs)
    rows, created, parents = [], 0, []
    for m in sorted(msgs, key=lambda m: ts_to_decimal(m.get("ts"))):
        res = ingest_message(db, conn, conv.channel_id, m, conv_type=conv.conv_type, source=source)
        if res.action in ("created", "updated"):
            rows.append(res.message)
        created += res.action == "created"
        if fetch_threads and int(m.get("reply_count") or 0) > 0 and m.get("thread_ts") == m.get("ts"):
            parents.append(m["ts"])
    for pts in parents:
        r, c = ingest_thread(db, client, conn, conv, pts, source)
        rows += r
        created += c
    return rows, created


def sync_conversation(db: Session, client: SlackClient, conn: SlackConnection, conv: SlackConversation,
                      on_progress: Callable[[], None] | None = None) -> int:
    """Backfill (resumable) if not complete, otherwise fetch everything newer than the high watermark."""
    tick = on_progress or (lambda: None)
    page_size = settings.slack_history_page_size
    initial_oldest = datetime_to_ts(utcnow() - timedelta(days=settings.slack_initial_sync_days))
    total = 0

    if not conv.backfill_complete:
        fetched = 0
        for msgs in fetch_conversation_history(client, conv.channel_id, oldest=initial_oldest, latest=conv.backfill_cursor_ts, page_size=page_size):
            rows, created = process_page(db, client, conn, conv, msgs)
            stamps = [m.get("ts") for m in msgs]
            if stamps:
                conv.backfill_cursor_ts = ts_min(conv.backfill_cursor_ts, *stamps)
                conv.backfill_top_ts = ts_max(conv.backfill_top_ts, *stamps)
            conv.messages_synced += created
            conn.messages_synced += created
            conv.last_synced_at = utcnow()
            db.commit()  # page + checkpoint are one transaction
            index_rows(db, rows)
            tick()
            total += created
            fetched += len(msgs)
            if fetched >= settings.slack_max_messages_per_conversation:
                log.warning("backfill_cap_reached channel=%s fetched=%s", conv.channel_id, fetched)
                break
        conv.backfill_complete, conv.backfill_cursor_ts, conv.last_error = True, None, None
        conv.high_watermark_ts = conv.backfill_top_ts or initial_oldest
        db.commit()
        return total

    oldest = conv.high_watermark_ts or initial_oldest
    new_top = oldest
    for msgs in fetch_conversation_history(client, conv.channel_id, oldest=oldest, page_size=page_size):
        rows, created = process_page(db, client, conn, conv, msgs)
        new_top = ts_max(new_top, *[m.get("ts") for m in msgs])
        conv.messages_synced += created
        conn.messages_synced += created
        db.commit()
        index_rows(db, rows)
        tick()
        total += created
    conv.high_watermark_ts, conv.last_synced_at, conv.last_error = new_top, utcnow(), None
    db.commit()
    return total


def reconcile_conversation(db: Session, client: SlackClient, conn: SlackConnection, conv: SlackConversation,
                           days: int | None = None, on_progress: Callable[[], None] | None = None) -> dict:
    """Re-read the last N days: apply edits, fetch missed thread replies, tombstone messages deleted in Slack."""
    tick = on_progress or (lambda: None)
    days = days or settings.slack_reconcile_days
    run_start = datetime_to_ts(utcnow())
    oldest = datetime_to_ts(utcnow() - timedelta(days=days))
    seen: set[str] = set()
    touched: list[SlackMessage] = []
    for msgs in fetch_conversation_history(client, conv.channel_id, oldest=oldest, latest=run_start):
        rows, _ = process_page(db, client, conn, conv, msgs, fetch_threads=False)
        touched += rows
        seen |= {m.get("ts") for m in msgs}
        tick()
    # thread replies for recent parents
    cutoff = utcnow() - timedelta(days=days)
    parents = (db.query(SlackMessage).filter(SlackMessage.connection_id == conn.id, SlackMessage.channel_id == conv.channel_id,
               SlackMessage.is_deleted.is_(False), SlackMessage.reply_count > 0, SlackMessage.thread_ts == SlackMessage.slack_ts,
               SlackMessage.message_created_at >= cutoff).order_by(SlackMessage.id.desc()).limit(MAX_THREADS_PER_RUN).all())
    for p in parents:
        r, _ = ingest_thread(db, client, conn, conv, p.slack_ts)
        touched += r
        tick()
    # deletions: stored top-level messages inside the fully-read window that Slack no longer returns
    deleted = 0
    window = (db.query(SlackMessage).filter(SlackMessage.connection_id == conn.id, SlackMessage.channel_id == conv.channel_id,
              SlackMessage.is_deleted.is_(False), SlackMessage.message_created_at >= ts_to_datetime(oldest),
              or_(SlackMessage.thread_ts.is_(None), SlackMessage.thread_ts == SlackMessage.slack_ts)).all())
    for row in window:
        if row.slack_ts not in seen and ts_to_decimal(oldest) < ts_to_decimal(row.slack_ts) < ts_to_decimal(run_start):
            res = apply_deletion(db, conn, conv.channel_id, row.slack_ts)
            if res.action == "deleted":
                touched.append(res.message)
                deleted += 1
    conv.last_synced_at, conv.last_error = utcnow(), None
    db.commit()
    index_rows(db, touched)
    return {"checked": len(seen), "deleted": deleted, "touched": len(touched)}


def resolve_unknown_actions(db: Session, conn: SlackConnection) -> int:
    """A timed-out chat.postMessage is 'unknown'. After a reconcile we can usually prove whether it was delivered."""
    import json
    resolved = 0
    pending = db.query(OutboundAction).filter(OutboundAction.connection_id == conn.id, OutboundAction.status.in_(("unknown", "sending"))).all()
    for act in pending:
        if act.status == "sending" and utcnow() - as_utc(act.created_at) < timedelta(minutes=2):
            continue  # still in flight
        req = json.loads(act.request_json or "{}")
        want = (req.get("text") or "").strip()
        since = as_utc(act.created_at) - timedelta(seconds=10)
        cands = db.query(SlackMessage).filter(SlackMessage.connection_id == conn.id, SlackMessage.channel_id == act.channel_id,
                                              SlackMessage.is_self.is_(True), SlackMessage.is_deleted.is_(False),
                                              SlackMessage.message_created_at >= since).all()
        hit = next((m for m in cands if m.raw_text.strip() == want or m.text == normalize_text(html.escape(want, quote=False))), None)
        if hit:
            act.status, act.external_id = "confirmed", hit.slack_ts
            resolved += 1
        elif utcnow() - as_utc(act.created_at) > timedelta(minutes=10):
            act.status, act.error = "unconfirmed", "Not found in Slack after reconcile; it was probably NOT delivered. Check the channel before resending."
            resolved += 1
    db.commit()
    return resolved


# -------------------------------------------------------------- top level ---
def sync_connection(db: Session, conn: SlackConnection, mode: str = "incremental", *, channel_id: str | None = None,
                    on_progress: Callable[[], None] | None = None, client_factory=None) -> dict:
    """Run a sync for one connection. mode: initial | incremental | reconcile."""
    tick = on_progress or (lambda: None)
    if conn.is_demo:
        conn.sync_state, conn.last_sync_finished_at = "idle", utcnow()
        db.commit()
        return {"demo": True, "note": "demo connections have no Slack API to sync"}
    conn.sync_state, conn.last_sync_started_at, conn.last_sync_error = "running", utcnow(), None
    db.commit()
    summary = {"conversations": 0, "new_messages": 0, "skipped": []}
    try:
        token = get_token(db, conn)
        with (client_factory or SlackClient)(token) as client:
            convs = discover_conversations(db, client, conn)
            if channel_id:
                convs = [c for c in convs if c.channel_id == channel_id]
            conn.sync_total_conversations, conn.sync_done_conversations = len(convs), 0
            db.commit()
            for conv in convs:
                try:
                    if mode == "reconcile" and conv.backfill_complete:
                        reconcile_conversation(db, client, conn, conv, on_progress=tick)
                    else:
                        summary["new_messages"] += sync_conversation(db, client, conn, conv, on_progress=tick)
                except (SlackChannelError, SlackScopeError) as exc:
                    db.rollback()
                    conv.last_error = f"{exc.code}: skipped"
                    if exc.code in ("not_in_channel", "channel_not_found"):
                        conv.is_active = False
                    db.commit()
                    summary["skipped"].append({"channel": conv.channel_id, "reason": exc.code})
                conn.sync_done_conversations += 1
                summary["conversations"] += 1
                db.commit()
                tick()
            if mode == "reconcile":
                resolve_unknown_actions(db, conn)
        conn.sync_state, conn.last_sync_finished_at = "idle", utcnow()
        log_event(db, "sync", "ok", f"{mode}: {summary['conversations']} conversations, {summary['new_messages']} new messages, {len(summary['skipped'])} skipped",
                  owner_id=conn.owner_id)
        schedule_discovery(db, conn.owner_id, delay_seconds=5)
        db.commit()
        return summary
    except (SlackAuthError, TokenCryptoError) as exc:
        db.rollback()
        mark_needs_reauth(db, conn, getattr(exc, "code", None) or str(exc))
        raise PermanentJobError(f"Slack authorization failed: {exc}") from exc
    except SlackRateLimited as exc:
        db.rollback()
        conn.sync_state, conn.last_sync_error = "rate_limited", f"Slack rate limit; resuming automatically in {exc.retry_after}s"
        db.commit()
        raise RescheduleJob(exc.retry_after, f"{exc.method} rate limited") from exc
    except Exception as exc:
        db.rollback()
        conn.sync_state, conn.last_sync_error = "error", f"{type(exc).__name__}: {exc}"[:500]
        db.commit()
        raise
