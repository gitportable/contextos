import hashlib
import json
import logging
import queue
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import desc, func, or_, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import assistant, auth, ratelimit
from app.cache import get_json, invalidate_user_caches, set_json
from app.config import settings
from app.crypto import TokenCryptoError
from app.database import SessionLocal, get_db, init_db
from app.integrations import create_calendar_event, index_messages, integration_status
from app.models import (EventReceipt, MessageProject, OutboundAction, ProcessingJob, Reminder, SlackConnection, SlackConversation, SlackMessage, SuggestedEvent,
                        User, utcnow)
from app.routes_account import router as account_router
from app.routes_auth import router as auth_router
from app.routes_pipeline import router as pipeline_router
from app.routes_projects import router as projects_router
from app.routes_gmail import router as gmail_router
from app.routes_google_calendar import router as google_calendar_router
from app.routes_intelligence import router as intelligence_router
from app.routes_items import router as items_router
from app.routes_slack import router as slack_router
from app.schemas import AssistantQuery, EventCreate, ReminderCreate, SlackChannelPost, SlackReply
from app.serializers import message_dicts
from app.slack_client import (SlackAmbiguousError, SlackApiError, SlackAuthError, SlackChannelError, SlackClient, SlackRateLimited,
                              SlackScopeError, SlackTransportError)
from app.slack_events import accept_event
from app.slack_oauth import get_token, mark_needs_reauth
from app.slack_signing import InvalidSlackSignature, verify_slack_request
from app.slack_sync import enqueue_sync

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("contextos")
STATIC_DIR = Path(__file__).parent / "static"
PRIORITIES = ("P0", "P1", "P2", "P3")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    problems = settings.production_problems()
    for line in problems:
        (logger.error if line.startswith("FATAL") else logger.warning)(line)
    if settings.is_production and any(p.startswith("FATAL") for p in problems):
        raise RuntimeError("Refusing to start in production with fatal configuration problems (see log above).")
    init_db()
    yield


app = FastAPI(title="ContextOS Pipeline API", version="0.5.0", lifespan=lifespan,
              docs_url="/docs" if not settings.is_production else None, redoc_url=None,
              openapi_url="/openapi.json" if not settings.is_production else None,
              description="Google sign-in, per-user Slack OAuth, durable queue, resumable sync, priority scoring, projects, LLM summaries, grounded assistant.")
for _r in (auth_router, slack_router, gmail_router, google_calendar_router, projects_router, pipeline_router, account_router, intelligence_router, items_router):
    app.include_router(_r)


def err(status: int, code: str, message: str, **extra):
    return HTTPException(status_code=status, detail={"code": code, "message": message, **extra})


# ------------------------------------------------------------- middleware ---
class BodyLimitMiddleware:
    """Pure-ASGI request-size cap: counts the bytes actually received (Content-Length can lie or be absent when chunked)."""
    def __init__(self, app, max_bytes: int):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        declared = dict(scope["headers"]).get(b"content-length")
        if declared and declared.isdigit() and int(declared) > self.max_bytes:
            return await JSONResponse(status_code=413, content={"detail": {"code": "too_large", "message": "Request body too large."}})(scope, receive, send)
        seen = 0

        async def limited():
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > self.max_bytes:
                    raise HTTPException(status_code=413, detail={"code": "too_large", "message": "Request body too large."})
            return msg
        await self.app(scope, limited, send)


CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https://lh3.googleusercontent.com; "
       "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    rid = request.headers.get("x-request-id", "")[:64] or uuid.uuid4().hex[:12]
    if request.method not in auth.SAFE_METHODS:
        # Defence in depth next to SameSite cookies + CSRF tokens: a browser always sends Origin on cross-site writes.
        origin = request.headers.get("origin")
        if origin and origin != "null":
            host = urlparse(origin).netloc.lower()
            allowed = {request.headers.get("host", "").lower(), urlparse(settings.public_base_url).netloc.lower()}
            if host not in allowed:
                return JSONResponse(status_code=403, content={"detail": {"code": "bad_origin", "message": "Cross-site request blocked."}})
        elif origin == "null":
            return JSONResponse(status_code=403, content={"detail": {"code": "bad_origin", "message": "Cross-site request blocked."}})
    try:
        response = await call_next(request)
    except HTTPException as exc:  # raised by BodyLimitMiddleware inside the stack
        response = JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    response.headers["X-Request-ID"] = rid
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = CSP
    if settings.cookie_secure:
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    path = request.url.path
    if not path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"  # authenticated JSON and the app shell must never sit in a shared cache
    return response


app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_body_bytes)


@app.exception_handler(ratelimit.RateLimited)
async def _rate_limited(_request: Request, exc: ratelimit.RateLimited):
    return ratelimit.rate_limit_response(exc)


@app.exception_handler(RequestValidationError)
async def _validation(_request: Request, exc: RequestValidationError):
    first = (exc.errors() or [{}])[0]
    field = ".".join(str(x) for x in first.get("loc", []) if x not in ("body", "query", "path"))
    return JSONResponse(status_code=422, content={"detail": {"code": "invalid_input", "message": f"Please check {field or 'your input'}: {first.get('msg', 'invalid value')}."}})


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    # Never leak stack traces, SQL or file paths to the client; the full error goes to the log with the request id.
    logger.exception("unhandled_error path=%s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": {"code": "server_error", "message": "Something went wrong on our side. Please try again."}})


# ------------------------------------------------------------------ basics ---
@app.get("/", include_in_schema=False)
def root():
    ui = STATIC_DIR / "index.html"
    return FileResponse(ui) if ui.exists() else {"app": "ContextOS", "status": "running"}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/api/config")
def public_config():
    return {"demo_enabled": settings.demo_enabled, "registration_open": settings.allow_registration and settings.allow_password_auth,
            "password_auth": settings.allow_password_auth, "google_login": settings.google_oauth_configured,
            "slack_oauth_configured": settings.slack_oauth_configured, "llm_enabled": settings.llm_enabled, "env": settings.app_env}


@app.get("/health")
def health():
    """Liveness: the process is up. No dependencies touched, so an orchestrator never restarts us because the DB blipped."""
    return {"status": "ok"}


@app.get("/ready")
def ready(db: Session = Depends(get_db)):
    """Readiness: can we serve traffic? Database is required; Redis is optional and only reported."""
    db.execute(text("SELECT 1"))
    return {"status": "ready"}


@app.get("/api/integrations")
def integrations(user: User = Depends(auth.require_admin)):
    return integration_status()


# ------------------------------------------------------------ slack events ---
@app.post("/integrations/slack/events")
async def slack_events(request: Request, db: Session = Depends(get_db),
                       x_slack_request_timestamp: str | None = Header(default=None), x_slack_signature: str | None = Header(default=None)):
    raw_body = await request.body()
    try:
        verify_slack_request(raw_body, x_slack_request_timestamp, x_slack_signature)
    except InvalidSlackSignature as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    try:
        payload: dict[str, Any] = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge", "")}
    if not payload.get("event_id"):
        raise HTTPException(status_code=400, detail="Missing event_id")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid payload")
    result = accept_event(db, payload)  # durable receipt + job before answering 200
    logger.info("slack_event %s", result)
    return result


@app.post("/integrations/slack/sync-history", include_in_schema=False)
def legacy_sync_history():
    raise err(410, "gone", "The admin-key history sync was replaced by per-user OAuth. Use Connect Slack, then Sync now.")


# ---------------------------------------------------------------- messages ---
def _inbox(db: Session, user: User, include_self: bool = False):
    q = db.query(SlackMessage).filter(SlackMessage.owner_id == user.id, SlackMessage.is_deleted.is_(False))
    return q if include_self else q.filter(SlackMessage.is_self.is_(False))


@app.get("/messages")
def list_messages(limit: int = 50, priority: str | None = None, include_self: bool = False, channel_id: str | None = None,
                  q: str | None = None, project_id: int | None = None, source: str | None = None, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    limit = max(1, min(limit, 200))
    if source not in (None, "slack", "gmail"):
        raise err(400, "bad_source", "source must be slack or gmail")
    priority_filter = priority.lower() if priority else None
    if priority_filter and priority_filter not in ("important", "urgent", "high", "normal") and priority.upper() not in PRIORITIES:
        raise err(400, "bad_priority", "priority must be important, urgent, high, normal, P0, P1, P2, or P3")
    q = (q or "").strip()[:200] or None
    params = f"{limit}|{(priority or 'all').upper()}|{int(include_self)}|{channel_id or ''}|{q or ''}|{project_id or ''}|{source or ''}"
    key = f"contextos:u{user.id}:messages:{hashlib.sha256(params.encode()).hexdigest()[:24]}"
    cached = get_json(key)
    if cached is not None:
        return cached
    query = _inbox(db, user, include_self)
    if priority_filter == "urgent":
        query = query.filter(SlackMessage.priority_label.in_(("P0", "P1")))
    elif priority_filter == "high":
        query = query.filter(SlackMessage.priority_label == "P1")
    elif priority_filter == "normal":
        query = query.filter(SlackMessage.priority_label.in_(("P2", "P3")))
    elif priority and priority_filter != "important":
        query = query.filter(SlackMessage.priority_label == priority.upper())
    if source:
        query = query.join(SlackConnection, SlackConnection.id == SlackMessage.connection_id).filter(SlackConnection.provider == source)
    if channel_id:
        query = query.filter(SlackMessage.channel_id == channel_id)
    if project_id:
        query = query.join(MessageProject, MessageProject.message_id == SlackMessage.id).filter(MessageProject.project_id == project_id)
    if q:
        esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.filter(SlackMessage.text.ilike(f"%{esc}%", escape="\\"))
    if priority_filter == "important" and not q:
        rows = []
        for label in PRIORITIES:
            row = (query.filter(SlackMessage.priority_label == label)
                   .order_by(desc(SlackMessage.priority_score), desc(SlackMessage.message_created_at))
                   .first())
            if row:
                rows.append(row)
    else:
        rows = query.order_by(desc(SlackMessage.priority_score), desc(SlackMessage.message_created_at)).limit(limit).all()
    result = message_dicts(db, rows)
    set_json(key, result, ttl=15)
    return result


def _own_message(db: Session, user: User, message_id: int) -> SlackMessage:
    row = db.query(SlackMessage).filter_by(id=message_id, owner_id=user.id).first()
    if not row:
        raise err(404, "not_found", "Message not found.")
    return row


@app.get("/messages/{message_id}")
def get_message(message_id: int, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    return message_dicts(db, [_own_message(db, user, message_id)])[0]


def _action_dict(a: OutboundAction) -> dict:
    return {"id": a.id, "type": a.action_type, "team_id": a.team_id, "channel_id": a.channel_id, "message_id": a.message_id,
            "status": a.status, "send_as": a.send_as, "external_id": a.external_id, "error": a.error, "created_at": a.created_at.isoformat()}


@app.post("/messages/{message_id}/reply")
def reply_to_slack(message_id: int, data: SlackReply, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    """Authenticated, ownership-checked, explicit-approval reply. Token chosen server-side from the user's own connection."""
    if not data.approved:
        raise err(409, "approval_required", "Reply not sent. Confirm the reviewed text before sending.")
    try:
        ratelimit.hit("reply:user", str(user.id), 60, 3600, "You've sent a lot of replies this hour. Please wait a bit.")
    except ratelimit.RateLimited as exc:
        raise ratelimit.ensure_http(exc) from exc
    row = _own_message(db, user, message_id)
    conn = db.query(SlackConnection).filter_by(id=row.connection_id, owner_id=user.id).first()
    if conn is not None and conn.provider != "slack":
        raise err(409, "read_only_source", "Email is read-only in ContextOS. Reply from your mail app.")
    if not conn or conn.status != "connected":
        raise err(409, "connection_inactive", "This Slack connection is not active. Reconnect Slack to reply.")
    key = (data.client_request_id or "").strip() or None
    if key:
        prior = db.query(OutboundAction).filter_by(owner_id=user.id, idempotency_key=key).first()
        if prior:
            if prior.status in ("sent", "confirmed", "simulated"):
                return {"ok": True, "replayed": True, **_action_dict(prior)}
            raise err(409, "duplicate_request", f"This reply request already exists with status '{prior.status}'. Check Slack before resending.", action_id=prior.id)
    thread_ts = data.thread_ts or row.thread_ts or row.slack_ts
    body = {"channel": row.channel_id, "text": data.text, "thread_ts": thread_ts}
    audit = OutboundAction(owner_id=user.id, connection_id=conn.id, idempotency_key=key, action_type="slack_reply", team_id=row.team_id,
                           channel_id=row.channel_id, message_id=row.id, send_as=data.send_as, request_json=json.dumps(body), status="sending")
    try:
        db.add(audit); db.commit()
    except IntegrityError:
        db.rollback()
        raise err(409, "duplicate_request", "This reply request is already being processed.")

    def fail(status: int, code: str, message: str, audit_status: str = "failed", **extra):
        audit.status, audit.error = audit_status, f"{code}: {message}"[:1000]
        db.commit()
        return err(status, code, message, action_id=audit.id, **extra)

    if conn.is_demo:
        audit.status, audit.external_id = "simulated", f"demo.{int(time.time())}"
        db.commit()
        return {"ok": True, "simulated": True, "note": "Demo connection: nothing was sent to Slack.", **_action_dict(audit)}
    if data.send_as == "user" and not conn.has_scope("chat:write", "user"):
        raise fail(403, "missing_scope", "Your Slack authorization lacks chat:write. Reconnect Slack and approve it.", needed="chat:write")
    if data.send_as == "bot" and not (conn.bot_token_encrypted and conn.has_scope("chat:write", "bot")):
        raise fail(403, "missing_scope", "No bot token with chat:write is available for this workspace.", needed="chat:write")
    try:
        token = get_token(db, conn, data.send_as)
        with SlackClient(token, max_inline_wait=0) as client:
            result = client.call("chat.postMessage", json_body=body, idempotent=False)
    except SlackAmbiguousError:
        audit.status, audit.error = "unknown", "Timed out; Slack may or may not have posted it. Do NOT resend blindly."
        enqueue_sync(db, conn, "reconcile", channel_id=row.channel_id)
        db.commit()
        raise err(504, "ambiguous", "Slack did not answer in time. The reply may have been posted; check the thread. ContextOS will reconcile and update the audit status.", action_id=audit.id)
    except SlackRateLimited as exc:
        raise fail(429, "ratelimited", f"Slack rate limit hit. Try again in {exc.retry_after}s.", retry_after=exc.retry_after)
    except (SlackAuthError, TokenCryptoError) as exc:
        if data.send_as == "user":
            mark_needs_reauth(db, conn, getattr(exc, "code", "token_error"))
        raise fail(409, "token_revoked", "Slack access expired or was revoked. Reconnect Slack.")
    except SlackScopeError as exc:
        raise fail(403, "missing_scope", "Slack says a permission is missing. Reconnect Slack and approve it.", needed=exc.needed)
    except SlackChannelError as exc:
        raise fail(409, exc.code, f"Slack refused the reply for this conversation ({exc.code}).")
    except (SlackTransportError, SlackApiError) as exc:
        raise fail(502, exc.code, f"Slack rejected the reply ({exc.code}).")
    audit.status, audit.external_id = "sent", str(result.get("ts", ""))
    enqueue_sync(db, conn, "reconcile", channel_id=row.channel_id)  # pull the new reply into the conversation
    db.commit()
    invalidate_user_caches(user.id)
    return {"ok": True, **_action_dict(audit), "channel": row.channel_id, "thread_ts": thread_ts}


@app.post("/channels/{connection_id}/{channel_id}/messages")
def post_to_slack_channel(connection_id: int, channel_id: str, data: SlackChannelPost,
                          user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    if not data.approved:
        raise err(409, "approval_required", "Message not sent. Review and confirm the text before sending.")
    if not data.text.strip():
        raise err(422, "empty_message", "Write a message before posting.")
    try:
        ratelimit.hit("reply:user", str(user.id), 60, 3600, "You've sent a lot of replies this hour. Please wait a bit.")
    except ratelimit.RateLimited as exc:
        raise ratelimit.ensure_http(exc) from exc
    conn = db.query(SlackConnection).filter_by(id=connection_id, owner_id=user.id, provider="slack").first()
    if not conn or conn.status != "connected":
        raise err(409, "connection_inactive", "This Slack connection is not active. Reconnect Slack to post.")
    conversation = db.query(SlackConversation).filter_by(
        connection_id=conn.id, channel_id=channel_id, is_active=True, is_archived=False,
    ).first()
    if not conversation or conversation.conv_type not in ("public_channel", "private_channel"):
        raise err(404, "channel_not_found", "That channel isn't available in your connected Slack workspace.")

    key = (data.client_request_id or "").strip() or None
    if key:
        prior = db.query(OutboundAction).filter_by(owner_id=user.id, idempotency_key=key).first()
        if prior:
            if prior.status in ("sent", "confirmed", "simulated"):
                return {"ok": True, "replayed": True, **_action_dict(prior)}
            raise err(409, "duplicate_request", f"This message request already exists with status '{prior.status}'. Check Slack before resending.", action_id=prior.id)
    body = {"channel": channel_id, "text": data.text}
    audit = OutboundAction(owner_id=user.id, connection_id=conn.id, idempotency_key=key,
                           action_type="slack_channel_post", team_id=conn.team_id, channel_id=channel_id,
                           send_as="user", request_json=json.dumps(body), status="sending")
    try:
        db.add(audit)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise err(409, "duplicate_request", "This message request is already being processed.")

    def fail(status: int, code: str, message: str, audit_status: str = "failed", **extra):
        audit.status, audit.error = audit_status, f"{code}: {message}"[:1000]
        db.commit()
        return err(status, code, message, action_id=audit.id, **extra)

    if conn.is_demo:
        audit.status, audit.external_id = "simulated", f"demo.{int(time.time())}"
        db.commit()
        return {"ok": True, "simulated": True, "note": "Demo connection: nothing was sent to Slack.", **_action_dict(audit)}
    if not conn.has_scope("chat:write", "user"):
        raise fail(403, "missing_scope", "Your Slack authorization lacks chat:write. Reconnect Slack and approve it.", needed="chat:write")
    try:
        token = get_token(db, conn, "user")
        with SlackClient(token, max_inline_wait=0) as client:
            result = client.call("chat.postMessage", json_body=body, idempotent=False)
    except SlackAmbiguousError:
        audit.status, audit.error = "unknown", "Timed out; Slack may or may not have posted it. Check the channel before retrying."
        enqueue_sync(db, conn, "reconcile", channel_id=channel_id)
        db.commit()
        raise err(504, "ambiguous", "Slack did not answer in time. Check the channel before retrying; ContextOS will reconcile.", action_id=audit.id)
    except SlackRateLimited as exc:
        raise fail(429, "ratelimited", f"Slack rate limit hit. Try again in {exc.retry_after}s.", retry_after=exc.retry_after)
    except (SlackAuthError, TokenCryptoError) as exc:
        mark_needs_reauth(db, conn, getattr(exc, "code", "token_error"))
        raise fail(409, "token_revoked", "Slack access expired or was revoked. Reconnect Slack.")
    except SlackScopeError as exc:
        raise fail(403, "missing_scope", "Slack says a permission is missing. Reconnect Slack and approve it.", needed=exc.needed)
    except SlackChannelError as exc:
        raise fail(409, exc.code, f"Slack refused the message for this channel ({exc.code}).")
    except (SlackTransportError, SlackApiError) as exc:
        raise fail(502, exc.code, f"Slack rejected the message ({exc.code}).")
    audit.status, audit.external_id = "sent", str(result.get("ts", ""))
    enqueue_sync(db, conn, "reconcile", channel_id=channel_id)
    db.commit()
    invalidate_user_caches(user.id)
    return {"ok": True, **_action_dict(audit), "channel": channel_id}


@app.get("/actions")
def list_actions(limit: int = 100, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    rows = db.query(OutboundAction).filter_by(owner_id=user.id).order_by(desc(OutboundAction.created_at)).limit(max(1, min(limit, 200))).all()
    return [_action_dict(r) for r in rows]


# ------------------------------------------------------------------- queue ---
def _conn_ids(db, user):
    return [i for (i,) in db.query(SlackConnection.id).filter_by(owner_id=user.id).all()]


def _user_jobs(db, user):
    ids = _conn_ids(db, user)
    teams = [t for (t,) in db.query(SlackConnection.team_id).filter_by(owner_id=user.id).all()]
    return db.query(ProcessingJob).filter(or_(ProcessingJob.connection_id.in_(ids), ProcessingJob.team_id.in_(teams), ProcessingJob.owner_id == user.id)), set(ids)


@app.get("/queue/summary")
def queue_summary(user: User = Depends(auth.require_admin), db: Session = Depends(get_db)):
    base, _ = _user_jobs(db, user)
    rows = base.with_entities(ProcessingJob.status, func.count(ProcessingJob.id)).group_by(ProcessingJob.status).all()
    return {"jobs_by_status": {s: c for s, c in rows}, "worker": "run `python -m app.worker` in a second terminal"}


@app.get("/queue/jobs")
def queue_jobs(status: str | None = None, limit: int = 100, user: User = Depends(auth.require_admin), db: Session = Depends(get_db)):
    base, mine = _user_jobs(db, user)
    if status:
        base = base.filter(ProcessingJob.status == status)
    rows = base.order_by(desc(ProcessingJob.created_at)).limit(max(1, min(limit, 200))).all()
    return [{"id": j.id, "job_type": j.job_type, "status": j.status, "attempts": j.attempts, "max_attempts": j.max_attempts,
             "available_at": j.available_at.isoformat(), "created_at": j.created_at.isoformat(),
             "last_error": j.last_error if (j.connection_id in mine or j.owner_id == user.id) else None} for j in rows]


def _metrics(db: Session, user: User) -> dict:
    base = _inbox(db, user, include_self=True)
    prios = {p: c for p, c in base.with_entities(SlackMessage.priority_label, func.count(SlackMessage.id)).group_by(SlackMessage.priority_label).all()}
    jobs_q, _ = _user_jobs(db, user)
    jobs = {s: c for s, c in jobs_q.with_entities(ProcessingJob.status, func.count(ProcessingJob.id)).group_by(ProcessingJob.status).all()}
    teams = [t for (t,) in db.query(SlackConnection.team_id).filter_by(owner_id=user.id).all()]
    acts = {s: c for s, c in db.query(OutboundAction.status, func.count(OutboundAction.id)).filter_by(owner_id=user.id).group_by(OutboundAction.status).all()}
    return {"messages": base.count(), "events_received": db.query(EventReceipt).filter(EventReceipt.team_id.in_(teams)).count() if teams else 0,
            "jobs": jobs, "priorities": {p: prios.get(p, 0) for p in PRIORITIES},
            "reminders_pending": db.query(Reminder).filter_by(owner_id=user.id, status="pending").count(),
            "suggestions_pending": db.query(SuggestedEvent).filter_by(owner_id=user.id, status="suggested").count(),
            "actions_sent": acts.get("sent", 0) + acts.get("confirmed", 0), "actions_failed": acts.get("failed", 0),
            "actions_unknown": acts.get("unknown", 0)}


@app.get("/api/pipeline/metrics")
def pipeline_metrics(user: User = Depends(auth.require_admin), db: Session = Depends(get_db)):
    return _metrics(db, user)


@app.get("/dashboard/summary")
def dashboard_summary(user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    m = _metrics(db, user)
    return {"messages_total": m["messages"], "by_priority": m["priorities"], "pending_reminders": m["reminders_pending"],
            "suggested_events": m["suggestions_pending"], "queued_jobs": m["jobs"].get("queued", 0), "failed_jobs": m["jobs"].get("failed", 0)}


# --------------------------------------------------------------- reminders ---
def _rem(r: Reminder) -> dict:
    return {"id": r.id, "message_id": r.message_id, "title": r.title, "due_at": r.due_at.isoformat(), "status": r.status}


@app.post("/reminders")
def create_reminder(data: ReminderCreate, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    if data.message_id:
        _own_message(db, user, data.message_id)
    due = data.due_at if data.due_at.tzinfo else data.due_at.replace(tzinfo=timezone.utc)
    row = Reminder(owner_id=user.id, title=data.title, due_at=due, message_id=data.message_id)
    db.add(row); db.commit(); db.refresh(row)
    return _rem(row)


@app.get("/reminders")
def list_reminders(status: str = "pending", limit: int = 100, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    q = db.query(Reminder).filter(Reminder.owner_id == user.id)
    q = q.filter(Reminder.status != "cancelled") if status == "all" else q.filter(Reminder.status == status)
    return [_rem(r) for r in q.order_by(Reminder.due_at).limit(max(1, min(limit, 200))).all()]


@app.post("/reminders/{reminder_id}/complete")
def complete_reminder(reminder_id: int, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    row = db.query(Reminder).filter_by(id=reminder_id, owner_id=user.id).first()
    if not row:
        raise err(404, "not_found", "Reminder not found.")
    row.status, row.completed_at = "completed", utcnow()
    db.commit()
    return {"ok": True, "id": row.id, "status": row.status}


# ---------------------------------------------------------------- schedule ---
def _ev(e: SuggestedEvent) -> dict:
    return {"id": e.id, "message_id": e.message_id, "title": e.title, "proposed_start": e.proposed_start.isoformat(),
            "duration_minutes": e.duration_minutes, "status": e.status, "source_text": e.source_text, "is_provisional": e.is_provisional}


def _own_event(db, user, event_id):
    row = db.query(SuggestedEvent).filter_by(id=event_id, owner_id=user.id).first()
    if not row:
        raise err(404, "not_found", "Suggestion not found.")
    return row


@app.get("/schedule/suggestions")
def list_suggestions(status: str = "suggested", user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    q = db.query(SuggestedEvent).filter(SuggestedEvent.owner_id == user.id)
    q = q.filter(SuggestedEvent.status != "cancelled") if status == "all" else q.filter(SuggestedEvent.status == status)
    return [_ev(e) for e in q.order_by(SuggestedEvent.proposed_start).limit(200).all()]


@app.post("/schedule/suggestions")
def create_schedule_suggestion(data: EventCreate, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    if data.message_id:
        _own_message(db, user, data.message_id)
    start = data.proposed_start if data.proposed_start.tzinfo else data.proposed_start.replace(tzinfo=timezone.utc)
    row = SuggestedEvent(owner_id=user.id, message_id=data.message_id, title=data.title, proposed_start=start,
                         duration_minutes=data.duration_minutes, source_text=data.source_text)
    db.add(row); db.commit(); db.refresh(row)
    return _ev(row)


@app.post("/schedule/suggestions/{event_id}/approve")
def approve_suggestion(event_id: int, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    row = _own_event(db, user, event_id)
    row.status = "approved"; db.commit()
    return {"ok": True, "id": row.id, "status": "approved", "note": "Approval recorded. Calendar creation needs Google Calendar OAuth."}


@app.post("/schedule/suggestions/{event_id}/approve-and-create")
def approve_and_create_calendar_event(event_id: int, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    row = _own_event(db, user, event_id)
    try:
        created = create_calendar_event(row.title, row.proposed_start, row.duration_minutes,
                                        f"Approved ContextOS suggestion from Slack message #{row.message_id}. Source: {row.source_text}")
    except Exception as exc:
        raise err(503, "calendar_unavailable", f"Calendar event not created: {exc}") from exc
    row.status = "created"; db.commit()
    return {"ok": True, "id": row.id, "status": "created", "calendar": created}


# --------------------------------------------------------------- assistant ---
@app.post("/assistant/ask/stream")
def assistant_ask_stream(data: AssistantQuery, user: User = Depends(auth.current_user)):
    owner_id = user.id
    try:
        ratelimit.hit("assistant:user", str(owner_id), settings.rl_assistant_user_per_hour, 3600,
                      "You've asked a lot of questions this hour. Please try again in a little while.")
    except ratelimit.RateLimited as exc:
        raise ratelimit.ensure_http(exc) from exc

    def events():
        updates: queue.Queue = queue.Queue()
        finished = object()

        def generate():
            db = SessionLocal()
            try:
                stream_user = db.query(User).filter_by(id=owner_id).first()
                if stream_user is None:
                    raise RuntimeError("The signed-in user no longer exists.")
                history = [{"role": turn.role, "content": turn.content} for turn in data.history]
                result = assistant.ask(db, stream_user, data.question.strip(),
                                       history=history,
                                       on_token=lambda token: updates.put(("token", {"text": token})))
                updates.put(("done", result))
            except Exception:
                logger.exception("assistant_stream_failed user_id=%s", owner_id)
                updates.put(("error", {"message": "The assistant could not finish this response. Please try again."}))
            finally:
                db.close()
                updates.put(finished)

        threading.Thread(target=generate, daemon=True).start()
        while True:
            item = updates.get()
            if item is finished:
                break
            event, payload = item
            yield f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/assistant/ask")
def assistant_ask(data: AssistantQuery, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    try:
        ratelimit.hit("assistant:user", str(user.id), settings.rl_assistant_user_per_hour, 3600,
                      "You've asked a lot of questions this hour. Please try again in a little while.")
    except ratelimit.RateLimited as exc:
        raise ratelimit.ensure_http(exc) from exc
    history = [{"role": turn.role, "content": turn.content} for turn in data.history]
    result = assistant.ask(db, user, data.question.strip(), history=history)
    result["vector_search"] = "configured" if settings.qdrant_enabled else "not_configured"
    return result


@app.post("/integrations/qdrant/reindex")
def qdrant_reindex(limit: int = 500, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    try:
        ratelimit.hit("expensive:user", str(user.id), settings.rl_expensive_user_per_hour, 3600)
    except ratelimit.RateLimited as exc:
        raise ratelimit.ensure_http(exc) from exc
    if not settings.qdrant_enabled:
        raise err(503, "not_configured", "Configure QDRANT_URL first (a bare 'https://' placeholder counts as unset).")
    rows = (_inbox(db, user, include_self=True).filter(SlackMessage.indexed_at.is_(None), SlackMessage.priority_label != "P3").order_by(SlackMessage.id.desc())
            .limit(max(1, min(limit, 5000))).all())
    ok, errors = 0, []
    for i in range(0, len(rows), 64):
        batch = rows[i:i + 64]
        res = index_messages(batch)
        if res.get("vector_ok"):
            now = utcnow()
            for r in batch:
                r.indexed_at = now
            ok += len(batch)
        else:
            errors.append(str(res.get("qdrant"))[:300])
    db.commit()
    return {"indexed": ok, "errors": errors[:5], "collection": settings.qdrant_collection}


# -------------------------------------------------------------------- demo ---
@app.post("/api/demo/seed")
def demo_seed(background: BackgroundTasks, size: str = "full", user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    if not settings.demo_enabled:
        raise err(403, "demo_disabled", "Demo data is disabled (set ENABLE_DEMO=true or APP_ENV=development).")
    if size not in ("small", "full"):
        raise err(400, "bad_size", "size must be small or full")
    try:
        ratelimit.hit("expensive:user", str(user.id), settings.rl_expensive_user_per_hour, 3600)
    except ratelimit.RateLimited as exc:
        raise ratelimit.ensure_http(exc) from exc
    from app.demo import seed_demo
    from app.worker import run_pending_jobs, worker_online
    result = seed_demo(db, user, size=size)
    if worker_online(db):
        result["processing"] = "worker"
    else:  # nobody is draining the queue: do it in this process, after the response, so the pipeline page can show it live
        background.add_task(run_pending_jobs, 20000, 90.0)
        result["processing"] = "in-process (no worker heartbeat found)"
    return result


@app.post("/api/demo/reset")
def demo_reset(user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    if not settings.demo_enabled:
        raise err(403, "demo_disabled", "Demo data is disabled.")
    from app.demo import remove_demo
    return remove_demo(db, user)
