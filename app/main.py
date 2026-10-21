import hmac
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import case, desc, func, or_, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import auth, google_auth, ratelimit, security
from app.validators import normalize_email, password_problems, valid_email
from app.trace import log_event
from app.summaries import esc_like, open_url
from app.routes_app import router as app_router
from app.cache import get_json, invalidate_user_caches, set_json
from app.config import settings
from app.crypto import TokenCryptoError
from app.database import get_db, init_db
from app.integrations import create_calendar_event, index_messages, integration_status
from app.models import (EventReceipt, Project, OutboundAction, ProcessingJob, Reminder, SlackConnection, SlackConversation,
                        SlackMessage, SlackUser, SuggestedEvent, User, UserSession, utcnow)
from app.schemas import (AssigneesUpdate, EventCreate, LoginIn, PasswordChange, ProfileUpdate, RegisterIn, ReminderCreate, SlackReply)
from app.slack_client import (SlackAmbiguousError, SlackApiError, SlackAuthError, SlackChannelError, SlackClient, SlackRateLimited,
                              SlackScopeError, SlackTransportError)
from app.slack_events import accept_event
from app.slack_oauth import get_token, mark_needs_reauth
from app.slack_signing import InvalidSlackSignature, verify_slack_request
from app.slack_sync import enqueue_sync
from app.routes_slack import router as slack_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("contextos")
init_db()
dev = settings.app_env == "development"
app = FastAPI(title="ContextOS Pipeline API", version="0.5.0", docs_url="/docs" if dev else None, redoc_url=None, openapi_url="/openapi.json" if dev else None,
              description="Per-user Slack OAuth, durable queue, resumable sync, priority scoring, reminders, schedule suggestions, grounded retrieval and approved Slack replies.")
security.install(app)
app.include_router(slack_router)
app.include_router(app_router)
PRIORITIES = ("P0", "P1", "P2", "P3")


def err(status: int, code: str, message: str, **extra):
    return HTTPException(status_code=status, detail={"code": code, "message": message, **extra})


# ------------------------------------------------------------------ basics ---
@app.get("/static/{name}", include_in_schema=False)
def static_file(name: str):
    f = (Path(__file__).parent / "static" / name).resolve()
    if f.parent != (Path(__file__).parent / "static").resolve() or not f.is_file() or f.suffix not in (".js", ".css", ".svg"):
        raise HTTPException(status_code=404)
    return FileResponse(f)


@app.get("/")
def root():
    ui = Path(__file__).parent / "static" / "index.html"
    return FileResponse(ui) if ui.exists() else {"app": "ContextOS", "status": "running", "docs": "/docs"}


@app.get("/api/config")
def public_config():
    return {"demo_enabled": settings.demo_enabled, "registration_open": settings.allow_registration,
            "google_enabled": settings.google_enabled}


@app.get("/health")
def health(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.get("/api/integrations")
def integrations(user: User = Depends(auth.admin_user)):
    return integration_status()


# -------------------------------------------------------------------- auth ---
def client_ip(request: Request) -> str:
    if settings.trust_proxy:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


def _session_response(db, user, response):
    user.last_login_at = utcnow()
    raw, sess = auth.create_session(db, user)
    old = db.query(auth.UserSession).filter_by(user_id=user.id).order_by(auth.UserSession.id.desc()).offset(10).all()  # cap sessions per user
    for o in old:
        db.delete(o)
    db.commit()
    auth.set_session_cookie(response, raw)
    return {"user": auth.user_payload(user), "csrf_token": sess.csrf_token}


@app.post("/auth/register")
def register(data: RegisterIn, request: Request, response: Response, db: Session = Depends(get_db)):
    if not settings.allow_registration:
        raise err(403, "registration_closed", "Registration is disabled.")
    ok, wait = ratelimit.hit(f"register:ip:{client_ip(request)}", 10, 3600)
    if not ok:
        raise err(429, "too_many_attempts", "Too many sign-up attempts from this network. Try again later.", retry_after=wait)
    email = normalize_email(data.email)
    if not valid_email(email):
        raise err(400, "bad_email", "Enter a valid email address, like name@example.com.")
    problems = password_problems(data.password, email)
    if problems:
        raise err(400, "weak_password", "Password needs: " + ", ".join(problems) + ".", problems=problems)
    user = User(email=email, name=(data.name.strip() or email.split("@")[0])[:200], password_hash=auth.hash_password(data.password))
    try:
        db.add(user); db.commit()
    except IntegrityError:
        db.rollback()
        raise err(409, "email_taken", "An account with that email already exists. Sign in instead.")
    log_event(db, "auth", "ok", "account created (password)", owner_id=user.id); db.commit()
    return _session_response(db, user, response)


@app.post("/auth/login")
def login(data: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    email, ip = normalize_email(data.email), client_ip(request)
    if not valid_email(email):
        raise err(400, "bad_email", "Enter a valid email address, like name@example.com.")
    pkey, ekey, ikey = f"login:pair:{email}:{ip}", f"login:email:{email}", f"login:ip:{ip}"
    wait = ratelimit.lock_remaining(pkey, ekey, ikey)
    if wait:
        raise err(429, "locked", f"Too many failed attempts. Try again in {wait // 60 + 1} min.", retry_after=wait)
    user = db.query(User).filter_by(email=email).first()
    ok = auth.verify_password(data.password, user.password_hash if user else "scrypt$16384$8$1$AAAA$AAAA")  # same cost either way
    if not user or not ok or not user.is_active:
        # 5 tries per (email, network) so an attacker cannot lock YOUR account from their own IP; 20 per email overall stops distributed guessing
        remaining, lock = ratelimit.record_failure(pkey, settings.login_max_attempts)
        _, lock_e = ratelimit.record_failure(ekey, settings.login_max_attempts * 4)
        ratelimit.record_failure(ikey, settings.login_ip_max_attempts)
        lock = lock or lock_e
        log_event(db, "auth", "error", "failed sign-in" + (" (account now locked)" if lock else ""), owner_id=user.id if user else None); db.commit()
        if lock:
            raise err(429, "locked", f"Too many failed attempts. Locked for {lock // 60} min.", retry_after=lock)
        raise err(401, "bad_credentials", f"Incorrect email or password. {remaining} attempt(s) left before a temporary lock.", attempts_left=remaining)
    ratelimit.clear_failures(pkey)
    log_event(db, "auth", "ok", "signed in (password)", owner_id=user.id)
    return _session_response(db, user, response)


@app.get("/auth/google/start")
def google_start(db: Session = Depends(get_db)):
    try:
        url, state = google_auth.start(db)
    except google_auth.GoogleAuthError as exc:
        return RedirectResponse(f"/?login=error&reason={exc.code}", status_code=302)
    resp = RedirectResponse(url, status_code=302)  # the state is also pinned to THIS browser, so a forged callback link cannot log a victim in as someone else
    resp.set_cookie("contextos_gstate", state, max_age=600, httponly=True, samesite="lax", secure=settings.cookie_secure, path="/auth/google")
    return resp


@app.get("/auth/google/callback")
def google_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None, db: Session = Depends(get_db)):
    ok, _ = ratelimit.hit(f"google:ip:{client_ip(request)}", 30, 600)
    if error or not ok:
        return RedirectResponse("/?login=error&reason=" + ("cancelled" if error else "rate_limited"), status_code=302)
    if not state or not hmac.compare_digest(request.cookies.get("contextos_gstate", ""), state):
        return RedirectResponse("/?login=error&reason=invalid_state", status_code=302)
    try:
        user = google_auth.finish(db, code, state)
    except google_auth.GoogleAuthError as exc:
        return RedirectResponse(f"/?login=error&reason={exc.code}", status_code=302)
    resp = RedirectResponse("/?welcome=1", status_code=302)
    resp.delete_cookie("contextos_gstate", path="/auth/google")
    _session_response(db, user, resp)
    log_event(db, "auth", "ok", "signed in with Google", owner_id=user.id); db.commit()
    return resp


@app.get("/auth/me")
def me(ctx=Depends(auth.optional_context)):
    if ctx is None:
        return {"authenticated": False}
    return {"authenticated": True, "user": auth.user_payload(ctx.user), "csrf_token": ctx.session.csrf_token}


@app.post("/auth/logout")
def logout(response: Response, ctx=Depends(auth.auth_context), db: Session = Depends(get_db)):
    db.delete(ctx.session); db.commit()
    auth.clear_session_cookie(response)
    return {"ok": True}


@app.patch("/auth/account")
def update_account(data: ProfileUpdate, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    name = data.name.strip()
    if not name:
        raise err(400, "bad_name", "Enter a display name.")
    user.name = name
    db.commit()
    return auth.user_payload(user)


@app.post("/auth/password")
def change_password(data: PasswordChange, ctx=Depends(auth.auth_context), db: Session = Depends(get_db)):
    user = ctx.user
    if user.password_hash == "!":
        raise err(409, "password_login_unavailable", "This account uses Google sign-in and has no password to change.")
    if not auth.verify_password(data.current_password, user.password_hash):
        raise err(400, "incorrect_password", "Your current password is incorrect.")
    problems = password_problems(data.new_password, user.email)
    if problems:
        raise err(400, "weak_password", "Password needs: " + ", ".join(problems) + ".", problems=problems)
    user.password_hash = auth.hash_password(data.new_password)
    db.query(UserSession).filter(UserSession.user_id == user.id, UserSession.id != ctx.session.id).delete(synchronize_session=False)
    db.commit()
    return {"ok": True}


@app.get("/auth/sessions")
def list_sessions(ctx=Depends(auth.auth_context), db: Session = Depends(get_db)):
    rows = db.query(UserSession).filter(UserSession.user_id == ctx.user.id, UserSession.expires_at > utcnow()).order_by(desc(UserSession.created_at)).all()
    return [{"id": row.id, "created_at": row.created_at.isoformat(), "expires_at": row.expires_at.isoformat(),
             "current": row.id == ctx.session.id} for row in rows]


@app.delete("/auth/sessions/{session_id}")
def revoke_session(session_id: int, ctx=Depends(auth.auth_context), db: Session = Depends(get_db)):
    row = db.query(UserSession).filter_by(id=session_id, user_id=ctx.user.id).first()
    if not row:
        raise err(404, "not_found", "Session not found.")
    if row.id == ctx.session.id:
        raise err(400, "current_session", "Sign out to end your current session.")
    db.delete(row)
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------ slack events ---
@app.post("/integrations/slack/events")
async def slack_events(request: Request, db: Session = Depends(get_db),
                       x_slack_request_timestamp: str | None = Header(default=None), x_slack_signature: str | None = Header(default=None)):
    raw_body = await request.body()
    if len(raw_body) > settings.max_body_bytes:
        raise HTTPException(status_code=413, detail="Payload too large")
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
    result = accept_event(db, payload)  # durable receipt + job before answering 200
    logger.info("slack_event %s", result)
    return result


@app.post("/integrations/slack/sync-history", include_in_schema=False)
def legacy_sync_history():
    raise err(410, "gone", "The admin-key history sync was replaced by per-user OAuth. Use Connect Slack, then Sync now.")


# ---------------------------------------------------------------- messages ---
from app.views import inbox as _inbox, message_dicts, own_message as _own_message


@app.get("/messages")
def list_messages(limit: int = 50, priority: str | None = None, include_self: bool = False, channel_id: str | None = None,
                  q: str | None = None, project_id: int | None = None, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    limit = max(1, min(limit, 200))
    if priority and priority.upper() not in PRIORITIES:
        raise HTTPException(status_code=400, detail="priority must be P0, P1, P2, or P3")
    q = (q or "").strip()[:100] or None
    key = f"contextos:u{user.id}:messages:{limit}:{(priority or 'all').upper()}:{int(include_self)}:{channel_id or ''}:{q or ''}:{project_id or ''}"
    cached = get_json(key)
    if cached is not None:
        return cached
    event_message_ids = {message_id for (message_id,) in db.query(SuggestedEvent.message_id).filter(
        SuggestedEvent.owner_id == user.id, SuggestedEvent.status == "suggested", SuggestedEvent.message_id.isnot(None)).all()}
    if include_self:
        query = _inbox(db, user, True)
    elif event_message_ids:
        query = _inbox(db, user, True).filter(or_(SlackMessage.is_self.is_(False), SlackMessage.id.in_(event_message_ids)))
    else:
        query = _inbox(db, user, False)
    if priority:
        query = query.filter(SlackMessage.priority_label == priority.upper())
    if channel_id:
        query = query.filter(SlackMessage.channel_id == channel_id)
    if project_id:
        query = query.filter(SlackMessage.project_id == project_id)
    if q:
        query = query.filter(SlackMessage.text.ilike(f"%{esc_like(q)}%", escape="\\"))
    ordering = [case((SlackMessage.id.in_(event_message_ids), 0), else_=1)] if event_message_ids else []
    rows = query.order_by(*ordering, desc(SlackMessage.priority_score), desc(SlackMessage.message_created_at)).limit(limit).all()
    result = message_dicts(db, rows)
    set_json(key, result, ttl=15)
    return result


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
    row = _own_message(db, user, message_id)
    conn = db.query(SlackConnection).filter_by(id=row.connection_id, owner_id=user.id).first()
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
    return db.query(ProcessingJob).filter(or_(ProcessingJob.connection_id.in_(ids), ProcessingJob.team_id.in_(teams))), set(ids)


@app.get("/queue/summary")
def queue_summary(user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    base, _ = _user_jobs(db, user)
    rows = base.with_entities(ProcessingJob.status, func.count(ProcessingJob.id)).group_by(ProcessingJob.status).all()
    return {"jobs_by_status": {s: c for s, c in rows}, "worker": "run `python -m app.worker` in a second terminal"}


@app.get("/queue/jobs")
def queue_jobs(status: str | None = None, limit: int = 100, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    base, mine = _user_jobs(db, user)
    if status:
        base = base.filter(ProcessingJob.status == status)
    rows = base.order_by(desc(ProcessingJob.created_at)).limit(max(1, min(limit, 200))).all()
    return [{"id": j.id, "job_type": j.job_type, "status": j.status, "attempts": j.attempts, "max_attempts": j.max_attempts,
             "available_at": j.available_at.isoformat(), "created_at": j.created_at.isoformat(),
             "last_error": j.last_error if j.connection_id in mine else None} for j in rows]


def _metrics(db: Session, user: User) -> dict:
    base = _inbox(db, user)
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
def pipeline_metrics(user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    return _metrics(db, user)


@app.get("/dashboard/summary")
def dashboard_summary(user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    m = _metrics(db, user)
    return {"messages_total": m["messages"], "by_priority": m["priorities"], "pending_reminders": m["reminders_pending"],
            "suggested_events": m["suggestions_pending"], "queued_jobs": m["jobs"].get("queued", 0), "failed_jobs": m["jobs"].get("failed", 0)}


# --------------------------------------------------------------- reminders ---
def _decode_assignees(raw: str) -> list[dict]:
    return json.loads(raw or "[]")


def _resolve_assignees(db: Session, user: User, ids: list[str]) -> str:
    ids = list(dict.fromkeys(ids))
    if len(ids) > 20:
        raise err(400, "too_many_assignees", "Choose no more than 20 people.")
    teams = [t for (t,) in db.query(SlackConnection.team_id).filter_by(owner_id=user.id, status="connected").all()]
    parsed = [value.split(":", 1) for value in ids]
    if any(len(pair) != 2 or pair[0] not in teams for pair in parsed):
        raise err(400, "invalid_assignee", "One or more selected people are not in your connected Slack workspace.")
    found = {(person.team_id, person.user_id): person for person in
             db.query(SlackUser).filter(SlackUser.team_id.in_(teams), SlackUser.is_bot.is_(False)).all()} if teams else {}
    result = []
    for team_id, slack_user_id in parsed:
        person = found.get((team_id, slack_user_id))
        if not person:
            raise err(400, "invalid_assignee", "One or more selected people are not in your connected Slack workspace.")
        result.append({"id": f"{team_id}:{slack_user_id}", "name": person.display_name,
                       "username": person.username, "email": person.email, "team": team_id})
    return json.dumps(result, separators=(",", ":"))


def _has_assignee(raw: str, person_id: str | None) -> bool:
    return not person_id or any(person["id"] == person_id for person in _decode_assignees(raw))


def _rem(r: Reminder) -> dict:
    return {"id": r.id, "message_id": r.message_id, "title": r.title, "due_at": r.due_at.isoformat(),
            "status": r.status, "assignees": _decode_assignees(r.assigned_people)}


@app.post("/reminders")
def create_reminder(data: ReminderCreate, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    if data.message_id:
        _own_message(db, user, data.message_id)
    due = data.due_at if data.due_at.tzinfo else data.due_at.replace(tzinfo=timezone.utc)
    row = Reminder(owner_id=user.id, title=data.title, due_at=due, message_id=data.message_id,
                   assigned_people=_resolve_assignees(db, user, data.assignees))
    db.add(row); db.commit(); db.refresh(row)
    return _rem(row)


@app.get("/reminders")
def list_reminders(status: str = "pending", limit: int = 100, person_id: str | None = None,
                   user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    q = db.query(Reminder).filter(Reminder.owner_id == user.id)
    q = q.filter(Reminder.status != "cancelled") if status == "all" else q.filter(Reminder.status == status)
    return [_rem(r) for r in q.order_by(Reminder.due_at).limit(max(1, min(limit, 500))).all()
            if _has_assignee(r.assigned_people, person_id)]


@app.post("/reminders/{reminder_id}/complete")
def complete_reminder(reminder_id: int, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    row = db.query(Reminder).filter_by(id=reminder_id, owner_id=user.id).first()
    if not row:
        raise err(404, "not_found", "Reminder not found.")
    row.status, row.completed_at = "completed", utcnow()
    db.commit()
    return {"ok": True, "id": row.id, "status": row.status}


@app.patch("/reminders/{reminder_id}/assignees")
def assign_reminder(reminder_id: int, data: AssigneesUpdate, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    row = db.query(Reminder).filter_by(id=reminder_id, owner_id=user.id).first()
    if not row:
        raise err(404, "not_found", "Reminder not found.")
    row.assigned_people = _resolve_assignees(db, user, data.assignees)
    db.commit()
    return _rem(row)


# ---------------------------------------------------------------- schedule ---
def _ev(e: SuggestedEvent, message: SlackMessage | None = None) -> dict:
    return {"id": e.id, "message_id": e.message_id, "title": e.title, "proposed_start": e.proposed_start.isoformat(),
            "duration_minutes": e.duration_minutes, "status": e.status, "source_text": e.source_text,
            "is_provisional": e.is_provisional, "assignees": _decode_assignees(e.assigned_people),
            "priority_label": message.priority_label if message else None,
            "priority_score": message.priority_score if message else None}


def _own_event(db, user, event_id):
    row = db.query(SuggestedEvent).filter_by(id=event_id, owner_id=user.id).first()
    if not row:
        raise err(404, "not_found", "Suggestion not found.")
    return row


@app.get("/schedule/suggestions")
def list_suggestions(status: str = "suggested", person_id: str | None = None,
                     user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    q = db.query(SuggestedEvent).filter(SuggestedEvent.owner_id == user.id)
    q = q.filter(SuggestedEvent.status != "cancelled") if status == "all" else q.filter(SuggestedEvent.status == status)
    events = q.order_by(SuggestedEvent.proposed_start).limit(500).all()
    message_ids = {event.message_id for event in events if event.message_id is not None}
    messages = {message.id: message for message in db.query(SlackMessage).filter(
        SlackMessage.owner_id == user.id, SlackMessage.id.in_(message_ids)).all()} if message_ids else {}
    result, seen = [], set()
    for event in events:
        if not _has_assignee(event.assigned_people, person_id):
            continue
        key = (event.title.casefold(), event.proposed_start.replace(second=0, microsecond=0), event.status)
        if key in seen:
            continue
        seen.add(key)
        result.append(_ev(event, messages.get(event.message_id)))
    return result


@app.post("/schedule/suggestions")
def create_schedule_suggestion(data: EventCreate, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    if data.message_id:
        _own_message(db, user, data.message_id)
    start = data.proposed_start if data.proposed_start.tzinfo else data.proposed_start.replace(tzinfo=timezone.utc)
    row = SuggestedEvent(owner_id=user.id, message_id=data.message_id, title=data.title, proposed_start=start,
                         duration_minutes=data.duration_minutes, source_text=data.source_text,
                         assigned_people=_resolve_assignees(db, user, data.assignees))
    db.add(row); db.commit(); db.refresh(row)
    return _ev(row)


@app.post("/schedule/suggestions/{event_id}/approve")
def approve_suggestion(event_id: int, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    row = _own_event(db, user, event_id)
    row.status = "approved"; db.commit()
    return {"ok": True, "id": row.id, "status": "approved", "note": "Approval recorded. Calendar creation needs Google Calendar OAuth."}


@app.patch("/schedule/suggestions/{event_id}/assignees")
def assign_event(event_id: int, data: AssigneesUpdate, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    row = _own_event(db, user, event_id)
    row.assigned_people = _resolve_assignees(db, user, data.assignees)
    db.commit()
    return _ev(row)


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


# (assistant, projects, today, pipeline: see routes_app.py)


@app.post("/integrations/qdrant/reindex")
def qdrant_reindex(limit: int = 500, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    if not settings.qdrant_enabled:
        raise err(503, "not_configured", "Configure QDRANT_URL first (a bare 'https://' placeholder counts as unset).")
    rows = (_inbox(db, user, include_self=True).filter(SlackMessage.indexed_at.is_(None)).order_by(SlackMessage.id.desc())
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
def demo_seed(process: bool = True, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    if not settings.demo_enabled:
        raise err(403, "demo_disabled", "Demo data is disabled (set ENABLE_DEMO=true or APP_ENV=development).")
    from app.demo import seed_demo
    return seed_demo(db, user, process=process)


@app.post("/api/demo/reset")
def demo_reset(user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    if not settings.demo_enabled:
        raise err(403, "demo_disabled", "Demo data is disabled.")
    from app.demo import remove_demo
    return remove_demo(db, user)
