"""Slack OAuth + connection management routes. Everything except /callback requires a signed-in ContextOS user."""
import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.config import ACCESS_LEVELS, DEFAULT_ACCESS_LEVEL, settings
from app import pipeline_events, ratelimit
from app.auth import AuthContext, current_user, optional_context
from app.cache import invalidate_user_caches
from app.database import get_db
from app.models import ProcessingJob, SlackConnection, SlackConversation, User, utcnow
from app.schemas import DisconnectRequest, SyncRequest
from app.pipeline import purge_connection_data
from app.slack_client import SlackClient
from app.slack_oauth import (SlackOAuthError, build_authorize_url, consume_state, create_state, exchange_code, get_token,
                             save_connection, wipe_tokens)
from app.slack_sync import enqueue_sync, missing_scopes, syncable_types

log = logging.getLogger("contextos.slack_routes")
router = APIRouter(prefix="/integrations/slack", tags=["slack"])


def connection_dict(db: Session, c: SlackConnection) -> dict:
    convs = db.query(SlackConversation).filter_by(connection_id=c.id).all()
    by_type: dict[str, int] = {}
    for cv in convs:
        if cv.is_active:
            by_type[cv.conv_type] = by_type.get(cv.conv_type, 0) + 1
    return {
        "id": c.id, "provider": c.provider, "access_level": c.access_level, "access_label": ACCESS_LEVELS.get(c.access_level, {}).get("label"), "team_id": c.team_id, "team_name": c.team_name, "slack_user_id": c.slack_user_id, "status": c.status,
        "is_demo": c.is_demo, "sync_state": c.sync_state, "conversations_total": c.sync_total_conversations,
        "conversations_done": c.sync_done_conversations, "conversations_by_type": by_type,
        "backfill_pending": sum(1 for cv in convs if cv.is_active and not cv.backfill_complete),
        "messages_synced": c.messages_synced,
        "last_sync_started_at": c.last_sync_started_at.isoformat() if c.last_sync_started_at else None,
        "last_sync_finished_at": c.last_sync_finished_at.isoformat() if c.last_sync_finished_at else None,
        "last_event_at": c.last_event_at.isoformat() if c.last_event_at else None,
        "last_error": c.last_sync_error, "granted_user_scopes": sorted(c.scopes("user")), "granted_bot_scopes": sorted(c.scopes("bot")),
        "missing_scopes": [] if c.is_demo else missing_scopes(c), "synced_conversation_types": syncable_types(c) if not c.is_demo else ["demo"],
        "can_reply_as_user": c.provider == "slack" and (c.is_demo or c.has_scope("chat:write", "user")),
        "can_reply_as_bot": bool(c.bot_token_encrypted) and c.has_scope("chat:write", "bot"),
        "connected_at": c.connected_at.isoformat() if c.connected_at else None,
        "problems": _problems(c),
    }


def _problems(c: SlackConnection) -> list[dict]:
    out = []
    if c.status in ("needs_reauth", "revoked"):
        out.append({"code": c.status, "message": "Slack access was revoked or expired. Reconnect Slack to resume syncing and replying."})
    if c.sync_state == "rate_limited":
        out.append({"code": "ratelimited", "message": c.last_sync_error or "Slack is rate limiting this workspace; the sync resumes automatically."})
    if not c.is_demo and c.status == "connected" and missing_scopes(c):
        out.append({"code": "missing_scope", "message": "Some permissions were not granted: " + ", ".join(missing_scopes(c)) + ". Reconnect to grant them."})
    if c.sync_state == "error" and c.last_sync_error and c.status == "connected":
        out.append({"code": "sync_error", "message": c.last_sync_error})
    return out


def _owned(db: Session, user: User, connection_id: int) -> SlackConnection:
    conn = db.query(SlackConnection).filter_by(id=connection_id, owner_id=user.id).first()
    if not conn:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Connection not found."})
    return conn


@router.get("/access-levels")
def access_levels(user: User = Depends(current_user)):
    """What each permission choice means. The UI renders this; scopes are shown for transparency."""
    return {"default": DEFAULT_ACCESS_LEVEL, "levels": [{"id": k, "label": v["label"], "scopes": v["scopes"]} for k, v in ACCESS_LEVELS.items()],
            "oauth_configured": settings.slack_oauth_configured}


@router.get("/connect")
def connect(level: str = DEFAULT_ACCESS_LEVEL, ctx: AuthContext = Depends(optional_context), db: Session = Depends(get_db)):
    if ctx is None:
        raise HTTPException(status_code=401, detail={"code": "not_authenticated", "message": "Sign in to ContextOS first, then click Connect Slack."})
    if level not in ACCESS_LEVELS:
        raise HTTPException(status_code=400, detail={"code": "bad_level", "message": "Unknown permission level."})
    try:
        ratelimit.hit("oauth:user", str(ctx.user.id), 20, 3600, "Too many Slack connection attempts. Try again later.")
        url = build_authorize_url(create_state(db, ctx.user, level), level)
    except ratelimit.RateLimited as exc:
        raise ratelimit.ensure_http(exc) from exc
    except SlackOAuthError as exc:
        raise HTTPException(status_code=503, detail={"code": exc.code, "message": str(exc)}) from exc
    return RedirectResponse(url, status_code=302)


@router.get("/callback")
def callback(code: str | None = None, state: str | None = None, error: str | None = None,
             ctx: AuthContext | None = Depends(optional_context), db: Session = Depends(get_db)):
    def back(status: str, reason: str = "") -> RedirectResponse:
        return RedirectResponse(f"/?slack={status}" + (f"&reason={quote(reason)}" if reason else ""), status_code=302)

    if error:  # user clicked Cancel on Slack's consent page
        return back("denied", error)
    if ctx is None:
        return back("error", "not_logged_in: open ContextOS through the same URL you registered in Slack (your ngrok URL) and sign in first")
    try:
        level = consume_state(db, state, ctx.user)
        if not code:
            raise SlackOAuthError("missing_code")
        conn, created = save_connection(db, ctx.user, exchange_code(code), level)
    except SlackOAuthError as exc:
        log.warning("slack_oauth_failed user=%s code=%s", ctx.user.id, exc.code)
        return back("error", exc.code)
    enqueue_sync(db, conn, "auto")
    pipeline_events.record(db, "slack_connected", "ok", owner_id=ctx.user.id, team_id=conn.team_id,
                           detail=f"{conn.team_name or conn.team_id}: access level '{conn.access_level}', {len(conn.scopes('user'))} permissions granted")
    db.commit()
    invalidate_user_caches(ctx.user.id)
    return back("connected")


@router.get("/connection")
def connection_status(user: User = Depends(current_user), db: Session = Depends(get_db)):
    conns = db.query(SlackConnection).filter(SlackConnection.owner_id == user.id, SlackConnection.status != "disconnected").order_by(SlackConnection.id).all()
    return {"connected": any(c.status == "connected" for c in conns), "oauth_configured": settings.slack_oauth_configured, "gmail_configured": settings.google_oauth_configured,
            "gmail_redirect_uri": f"{settings.public_base_url.rstrip('/')}/integrations/gmail/callback",
            "redirect_uri": settings.redirect_uri, "connections": [connection_dict(db, c) for c in conns]}


@router.get("/channels")
def list_postable_channels(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = (db.query(SlackConnection, SlackConversation)
            .join(SlackConversation, SlackConversation.connection_id == SlackConnection.id)
            .filter(SlackConnection.owner_id == user.id, SlackConnection.provider == "slack",
                    SlackConnection.status == "connected", SlackConversation.is_active.is_(True),
                    SlackConversation.is_archived.is_(False),
                    SlackConversation.conv_type.in_(("public_channel", "private_channel")))
            .order_by(SlackConversation.name, SlackConversation.channel_id).all())
    return {"channels": [
        {"connection_id": conn.id, "channel_id": conv.channel_id,
         "channel_name": conv.name or conv.channel_id,
         "can_post": conn.is_demo or conn.has_scope("chat:write", "user")}
        for conn, conv in rows
    ]}


@router.post("/sync")
def start_sync(data: SyncRequest | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    data = data or SyncRequest()
    q = db.query(SlackConnection).filter(SlackConnection.owner_id == user.id, SlackConnection.status == "connected")
    if data.connection_id is not None:
        q = q.filter(SlackConnection.id == data.connection_id)
    conns = q.all()
    if not conns:
        raise HTTPException(status_code=409, detail={"code": "no_connection", "message": "No active Slack connection. Connect Slack first."})
    results = []
    for c in conns:
        if c.is_demo:
            results.append({"connection_id": c.id, "status": "demo", "message": "Demo connections have no live Slack to sync."})
            continue
        job = enqueue_sync(db, c, data.mode)
        results.append({"connection_id": c.id, "status": "queued" if job else "already_queued", "job_id": job.id if job else None})
    db.commit()
    return {"ok": True, "results": results, "note": "The worker (python -m app.worker) executes the sync; progress shows in the Slack panel."}


@router.post("/disconnect")
def disconnect(data: DisconnectRequest, user: User = Depends(current_user), db: Session = Depends(get_db)):
    conn = _owned(db, user, data.connection_id)
    revoked = False
    if conn.status == "connected" and conn.provider == "gmail":
        from app import gmail
        revoked = gmail.revoke(db, conn)
    elif conn.status == "connected" and not conn.is_demo:
        try:  # revoke the USER token only; the bot token is shared by everyone who installed the app in that workspace
            with SlackClient(get_token(db, conn), max_inline_wait=2, retries=1) as client:
                client.call("auth.revoke")
            revoked = True
        except Exception as exc:
            log.info("slack_revoke_skipped conn=%s reason=%s", conn.id, type(exc).__name__)
    wipe_tokens(conn)
    conn.status, conn.sync_state, conn.revoked_at = "disconnected", "idle", utcnow()
    cancelled = (db.query(ProcessingJob).filter(ProcessingJob.connection_id == conn.id, ProcessingJob.status == "queued")
                 .update({"status": "failed", "last_error": "connection disconnected", "dedupe_key": None, "finished_at": utcnow()}))
    purged = 0
    if data.purge:
        purged = purge_connection_data(db, conn)
    db.commit()
    invalidate_user_caches(user.id)
    return {"ok": True, "status": "disconnected", "slack_token_revoked": revoked, "queued_jobs_cancelled": cancelled, "messages_deleted": purged}
