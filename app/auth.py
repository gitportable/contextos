"""Minimal real authentication: scrypt passwords + server-side sessions + CSRF token for state-changing calls."""
import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import User, UserSession, as_utc, utcnow

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_N, _R, _P = 2 ** 14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return "scrypt${}${}${}${}${}".format(_N, _R, _P, base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(digest, base64.b64decode(digest_b64))
    except Exception:
        return False


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def normalize_email(email: str) -> str:
    return email.strip().lower()


def create_session(db: Session, user: User) -> tuple[str, UserSession]:
    raw = secrets.token_urlsafe(32)
    row = UserSession(user_id=user.id, token_hash=_hash_token(raw), csrf_token=secrets.token_urlsafe(24),
                      expires_at=utcnow() + timedelta(hours=settings.session_ttl_hours))
    db.add(row)
    db.commit()
    return raw, row


def set_session_cookie(response: Response, raw_token: str) -> None:
    response.set_cookie(settings.session_cookie_name, raw_token, max_age=settings.session_ttl_hours * 3600,
                        httponly=True, samesite="lax", secure=settings.cookie_secure, path="/")


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(settings.session_cookie_name, path="/")


@dataclass
class AuthContext:
    user: User
    session: UserSession


def _load_context(request: Request, db: Session) -> AuthContext | None:
    raw = request.cookies.get(settings.session_cookie_name)
    if not raw:
        return None
    sess = db.query(UserSession).filter_by(token_hash=_hash_token(raw)).first()
    if not sess or as_utc(sess.expires_at) <= utcnow():
        return None
    user = db.query(User).filter_by(id=sess.user_id).first()
    if not user or not user.is_active:
        return None
    return AuthContext(user=user, session=sess)


def optional_context(request: Request, db: Session = Depends(get_db)) -> AuthContext | None:
    """For browser navigations (OAuth callback): no CSRF check because the request is a GET."""
    return _load_context(request, db)


def auth_context(request: Request, db: Session = Depends(get_db)) -> AuthContext:
    ctx = _load_context(request, db)
    if not ctx:
        raise HTTPException(status_code=401, detail={"code": "not_authenticated", "message": "Sign in to continue."})
    if request.method not in SAFE_METHODS:
        sent = request.headers.get("x-csrf-token", "")
        if not sent or not hmac.compare_digest(sent, ctx.session.csrf_token):
            raise HTTPException(status_code=403, detail={"code": "csrf", "message": "Missing or invalid CSRF token. Reload the page."})
    return ctx


def current_user(ctx: AuthContext = Depends(auth_context)) -> User:
    return ctx.user


def is_admin(user: User) -> bool:
    return bool(settings.admin_email and hmac.compare_digest(
        normalize_email(user.email), normalize_email(settings.admin_email)
    ))


def user_payload(user: User) -> dict:
    return {"id": user.id, "email": user.email, "name": user.name, "avatar_url": user.avatar_url,
            "is_admin": is_admin(user), "has_password": user.password_hash != "!"}


def admin_user(user: User = Depends(current_user)) -> User:
    if not is_admin(user):
        raise HTTPException(status_code=403, detail={"code": "admin_required", "message": "Admin access is required."})
    return user
