"""Slack OAuth + connection management routes. Everything except /callback requires a signed-in ContextOS user."""
import logging
from datetime import timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.auth import AuthContext, current_user, optional_context
from app.cache import invalidate_user_caches
from app.database import get_db
from app.models import ProcessingJob, SlackConnection, SlackConversation, SlackUser, User, as_utc, utcnow
from app.schemas import DisconnectRequest, SyncRequest
from app.pipeline import purge_connection_data
from app.slack_client import SlackApiError, SlackAuthError, SlackClient, SlackRateLimited, SlackScopeError
from app.trace import log_event
from app.slack_oauth import (PRESETS, SlackOAuthError, mark_needs_reauth, scopes_for, build_authorize_url, consume_state, create_state, exchange_code, get_token,
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
        "id": c.id, "team_id": c.team_id, "team_name": c.team_name, "slack_user_id": c.slack_user_id, "status": c.status,
        "is_demo": c.is_demo, "sync_state": c.sync_state, "conversations_total": c.sync_total_conversations,
        "conversations_done": c.sync_done_conversations, "conversations_by_type": by_type,
        "backfill_pending": sum(1 for cv in convs if cv.is_active and not cv.backfill_complete),
        "messages_synced": c.messages_synced,
        "last_sync_started_at": c.last_sync_started_at.isoformat() if c.last_sync_started_at else None,
        "last_sync_finished_at": c.last_sync_finished_at.isoformat() if c.last_sync_finished_at else None,
        "last_event_at": c.last_event_at.isoformat() if c.last_event_at else None,
        "last_error": c.last_sync_error, "granted_user_scopes": sorted(c.scopes("user")), "granted_bot_scopes": sorted(c.scopes("bot")),
        "missing_scopes": [] if c.is_demo else missing_scopes(c), "can_read_private": c.has_scope("groups:history"), "synced_conversation_types": syncable_types(c) if not c.is_demo else ["demo"],
        "can_reply_as_user": c.is_demo or c.has_scope("chat:write", "user"), "slack_email": c.slack_email,
        "reply_mode": "reply_in_contextos" if (c.is_demo or c.has_scope("chat:write", "user")) else "open_in_slack",
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


@router.get("/connect")
def connect(preset: str = "essentials", reply: bool = False, keep: bool = False,
            ctx: AuthContext = Depends(optional_context), db: Session = Depends(get_db)):
    """preset: essentials (public channels + DMs) | everything (+ private channels, group DMs). reply=true adds chat:write.
    keep=true re-requests whatever the user already granted (used by 'Enable replying'). Scopes are decided HERE, never by the browser."""
    if ctx is None:
        raise HTTPException(status_code=401, detail={"code": "not_authenticated", "message": "Sign in to ContextOS first, then click Connect Slack."})
    if preset not in PRESETS:
        raise HTTPException(status_code=400, detail={"code": "bad_preset", "message": "Unknown permission preset."})
    kept: set[str] = set()
    if keep:
        for c in db.query(SlackConnection).filter_by(owner_id=ctx.user.id).all():
            kept |= c.scopes("user")
    try:
        url = build_authorize_url(create_state(db, ctx.user, scopes_for(preset, reply, kept)), scopes_for(preset, reply, kept))
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
        requested = consume_state(db, state, ctx.user)
        if not code:
            raise SlackOAuthError("missing_code")
        conn, created = save_connection(db, ctx.user, exchange_code(code), requested)
    except SlackOAuthError as exc:
        log.warning("slack_oauth_failed user=%s code=%s", ctx.user.id, exc.code)
        return back("error", exc.code)
    if conn.has_scope("users:read.email"):  # show which Slack identity was linked; best effort
        try:
            with SlackClient(get_token(db, conn), max_inline_wait=2, retries=1) as client:
                conn.slack_email = ((client.call("users.info", {"user": conn.slack_user_id}).get("user") or {}).get("profile") or {}).get("email")
        except Exception as exc:
            log.info("slack_email_lookup_skipped reason=%s", type(exc).__name__)
    enqueue_sync(db, conn, "auto")
    log_event(db, "slack_oauth", "ok", f"workspace {conn.team_name or conn.team_id} connected; scopes: {conn.granted_user_scopes}", owner_id=ctx.user.id)
    db.commit()
    invalidate_user_caches(ctx.user.id)
    return back("connected")


@router.get("/connection")
def connection_status(user: User = Depends(current_user), db: Session = Depends(get_db)):
    conns = db.query(SlackConnection).filter_by(owner_id=user.id).order_by(SlackConnection.id).all()
    return {"connected": any(c.status == "connected" for c in conns),
            "connections": [connection_dict(db, c) for c in conns]}


@router.get("/people")
def slack_people(q: str = "", user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Return and refresh the Slack directory for this user's own connected workspaces."""
    conns = db.query(SlackConnection).filter_by(owner_id=user.id, status="connected").all()
    now, refresh_after = utcnow(), utcnow() - timedelta(days=1)
    for conn in conns:
        if conn.is_demo or (conn.people_synced_at and as_utc(conn.people_synced_at) >= refresh_after):
            continue
        if not conn.has_scope("users:read"):
            raise HTTPException(status_code=409, detail={"code": "missing_scope", "message": "Reconnect Slack and grant users:read to load your workspace people."})
        try:
            with SlackClient(get_token(db, conn), max_inline_wait=2, retries=1) as client:
                existing = {person.user_id: person for person in
                            db.query(SlackUser).filter_by(team_id=conn.team_id).all()}
                for page in client.paginate("users.list", {"limit": 200}):
                    for slack_user in page.get("members", []):
                        uid = slack_user.get("id")
                        if not uid or slack_user.get("deleted") or slack_user.get("is_bot"):
                            continue
                        profile = slack_user.get("profile") or {}
                        name = (profile.get("display_name") or slack_user.get("real_name")
                                or profile.get("real_name") or slack_user.get("name") or uid)
                        person = existing.get(uid)
                        if person is None:
                            person = SlackUser(team_id=conn.team_id, user_id=uid)
                            db.add(person)
                            existing[uid] = person
                        person.display_name = name
                        person.username = slack_user.get("name") or ""
                        person.email = profile.get("email")
                        person.is_bot = False
                        person.updated_at = now
            conn.people_synced_at = now
            db.commit()
        except SlackAuthError as exc:
            mark_needs_reauth(db, conn, exc.code)
            db.commit()
            raise HTTPException(status_code=409, detail={"code": "needs_reauth", "message": "Reconnect Slack to refresh your workspace people."}) from exc
        except SlackScopeError as exc:
            raise HTTPException(status_code=409, detail={"code": "missing_scope", "message": "Reconnect Slack and grant users:read to load your workspace people."}) from exc
        except SlackRateLimited as exc:
            raise HTTPException(status_code=429, detail={"code": "ratelimited", "message": f"Slack asked us to retry in {exc.retry_after} seconds."}) from exc
        except SlackApiError as exc:
            raise HTTPException(status_code=503, detail={"code": "slack_directory_unavailable", "message": f"Slack directory could not be loaded ({exc.code})."}) from exc

    teams = [conn.team_id for conn in conns]
    if not teams:
        return []
    rows = db.query(SlackUser).filter(SlackUser.team_id.in_(teams), SlackUser.is_bot.is_(False)).order_by(SlackUser.display_name).limit(5000).all()
    query = q.strip().casefold()[:100]
    results = []
    for person in rows:
        if query and query not in " ".join((person.display_name, person.username, person.email or "")).casefold():
            continue
        results.append({"id": f"{person.team_id}:{person.user_id}", "name": person.display_name,
                        "username": person.username, "email": person.email, "team_id": person.team_id})
    return results[:500]


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
    if conn.status == "connected" and not conn.is_demo:
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
