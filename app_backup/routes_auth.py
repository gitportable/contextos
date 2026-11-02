"""Sign-in routes: Google (primary) and email + password (optional). Every entry point is rate limited."""
import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import auth, google_auth, pipeline_events, ratelimit
from app.config import settings
from app.database import get_db
from app.models import User
from app.schemas import LoginIn, RegisterIn

log = logging.getLogger("contextos.auth")
router = APIRouter(tags=["auth"])
COMMON_PASSWORDS = {"password12", "password123", "1234567890", "qwertyuiop", "letmein123", "iloveyou12", "welcome123", "admin12345", "contextos123"}
_DUMMY_HASH = auth.hash_password("dummy-password-for-timing")  # real scrypt params: unknown-email logins cost the same CPU


def _err(status: int, code: str, message: str, **extra):
    from fastapi import HTTPException
    return HTTPException(status_code=status, detail={"code": code, "message": message, **extra})


def user_dict(u: User, db: Session | None = None) -> dict:
    return {"id": u.id, "email": u.email, "name": u.name, "avatar_url": u.avatar_url, "signed_in_with_google": bool(u.google_sub),
            "is_admin": bool(db is not None and auth.is_admin(db, u))}


def _login_done(db: Session, response, user: User, method: str) -> dict:
    raw, sess = auth.create_session(db, user)
    auth.set_session_cookie(response, raw)
    pipeline_events.record(db, "login", "ok", owner_id=user.id, detail=f"signed in with {method}")
    db.commit()
    return {"user": user_dict(user, db), "csrf_token": sess.csrf_token}


# ------------------------------------------------------------------ google ---
@router.get("/auth/google/start")
def google_start(request: Request, db: Session = Depends(get_db)):
    try:
        ratelimit.hit("oauth:ip", ratelimit.client_ip(request), settings.rl_oauth_ip_per_5min, 300)
        url, binder = google_auth.start(db)
    except ratelimit.RateLimited as exc:
        return RedirectResponse(f"/?login=error&reason=rate_limited&wait={exc.retry_after}", status_code=302)
    except google_auth.GoogleAuthError as exc:
        return RedirectResponse(f"/?login=error&reason={quote(exc.code)}", status_code=302)
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie(google_auth.BINDER_COOKIE, binder, max_age=600, httponly=True, samesite="lax", secure=settings.cookie_secure, path="/auth/google")
    return resp


@router.get("/auth/google/callback")
def google_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None,
                    db: Session = Depends(get_db)):
    def done(target: str) -> RedirectResponse:
        r = RedirectResponse(target, status_code=302)
        r.delete_cookie(google_auth.BINDER_COOKIE, path="/auth/google")
        return r

    try:
        ratelimit.hit("oauth:ip", ratelimit.client_ip(request), settings.rl_oauth_ip_per_5min, 300)
    except ratelimit.RateLimited as exc:
        return done(f"/?login=error&reason=rate_limited&wait={exc.retry_after}")
    if error:  # the user pressed Cancel on Google's page
        return done("/?login=denied")
    try:
        user, created = google_auth.finish(db, code, state, request.cookies.get(google_auth.BINDER_COOKIE))
    except google_auth.GoogleAuthError as exc:
        log.warning("google_login_failed code=%s", exc.code)
        return done(f"/?login=error&reason={quote(exc.code)}")
    resp = done("/?login=ok" + ("&new=1" if created else ""))
    _login_done(db, resp, user, "Google")
    return resp


# ---------------------------------------------------------------- password ---
def _check_password_policy(email: str, password: str) -> None:
    local = email.split("@")[0]
    if password.lower() in COMMON_PASSWORDS or (len(local) >= 4 and local in password.lower()) or len(set(password)) < 5:
        raise _err(400, "weak_password", "Choose a stronger password (not a common one, and not containing your email name).")


@router.post("/auth/register")
def register(data: RegisterIn, request: Request, response: Response, db: Session = Depends(get_db)):
    if not (settings.allow_registration and settings.allow_password_auth):
        raise _err(403, "registration_closed", "Password sign-up is disabled. Use Continue with Google.")
    ip = ratelimit.client_ip(request)
    try:
        ratelimit.hit("register:ip", ip, settings.rl_register_ip_per_hour, 3600, "Too many sign-ups from this network. Try again later.")
    except ratelimit.RateLimited as exc:
        raise ratelimit.ensure_http(exc) from exc
    try:
        email = auth.validate_email(data.email)
    except auth.InvalidEmail as exc:
        raise _err(400, "bad_email", str(exc)) from exc
    _check_password_policy(email, data.password)
    user = User(email=email, name=(data.name or "").strip()[:200] or email.split("@")[0], password_hash=auth.hash_password(data.password),
                email_verified=False)
    try:
        db.add(user)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise _err(409, "email_taken", "An account with that email already exists. Try signing in, or continue with Google.")
    return _login_done(db, response, user, "password")


@router.post("/auth/login")
def login(data: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    if not settings.allow_password_auth:
        raise _err(403, "password_disabled", "Password sign-in is disabled. Use Continue with Google.")
    ip = ratelimit.client_ip(request)
    try:
        ratelimit.login_ip_gate(ip)
        try:
            email = auth.validate_email(data.email)
        except auth.InvalidEmail as exc:
            raise _err(400, "bad_email", str(exc)) from exc
        ratelimit.login_account_gate(email, ip)
    except ratelimit.RateLimited as exc:
        raise ratelimit.ensure_http(exc) from exc
    user = db.query(User).filter_by(email=email).first()
    ok = auth.verify_password(data.password, user.password_hash if user and user.password_hash else _DUMMY_HASH)
    if not user or not user.password_hash or not ok or not user.is_active:
        ratelimit.login_failed(email, ip)
        raise _err(401, "bad_credentials", "Incorrect email or password. If you normally use Google, choose Continue with Google.")
    ratelimit.login_succeeded(email, ip)
    return _login_done(db, response, user, "password")


@router.get("/auth/me")
def me(ctx=Depends(auth.optional_context), db: Session = Depends(get_db)):
    if ctx is None:
        return {"authenticated": False}
    return {"authenticated": True, "user": user_dict(ctx.user, db), "csrf_token": ctx.session.csrf_token}


@router.post("/auth/logout")
def logout(response: Response, ctx=Depends(auth.auth_context), db: Session = Depends(get_db)):
    db.delete(ctx.session)
    db.commit()
    auth.clear_session_cookie(response)
    return {"ok": True}
