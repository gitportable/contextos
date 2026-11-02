"""Authentication primitives: strict email validation, scrypt passwords, server-side sessions, CSRF token for
state-changing calls, and a per-user API rate limit on every authenticated request."""
import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import ratelimit
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


class InvalidEmail(ValueError):
    pass


_LOCAL_RE = re.compile(r"^[a-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*$")
_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def validate_email(raw: str) -> str:
    """Return the normalised address or raise InvalidEmail with a message that is safe to show to the user.

    Deliberately stricter than RFC 5322: ASCII only (no look-alike Unicode domains / homoglyph account takeovers),
    one '@', dot-atom local part, DNS-shaped domain with a real alphabetic TLD. Deliverability is NOT checked (no network);
    Google sign-in proves ownership, password sign-ups stay `email_verified=False`.
    """
    email = normalize_email(raw)
    if not email:
        raise InvalidEmail("Enter your email address.")
    if len(email) > 254 or not email.isascii() or any(c.isspace() or ord(c) < 32 for c in email):
        raise InvalidEmail("That doesn't look like an email address. Use letters, numbers and . _ - + only, with no spaces.")
    if email.count("@") != 1:
        raise InvalidEmail("An email address needs exactly one @, like name@example.com.")
    local, domain = email.split("@")
    if not local or len(local) > 64 or not _LOCAL_RE.match(local):
        raise InvalidEmail("The part before the @ isn't valid (no spaces, no doubled or leading/trailing dots).")
    labels = domain.split(".")
    if len(labels) < 2 or not all(_LABEL_RE.match(l) for l in labels):
        raise InvalidEmail("The part after the @ isn't valid. It should look like example.com.")
    tld = labels[-1]
    if len(tld) < 2 or not re.fullmatch(r"[a-z]{2,24}|xn--[a-z0-9-]{2,59}", tld):
        raise InvalidEmail("The ending of the address (like .com) isn't valid.")
    return email


def create_session(db: Session, user: User) -> tuple[str, UserSession]:
    """New random token on every sign-in (no session fixation). The oldest sessions beyond the per-user cap are dropped."""
    raw = secrets.token_urlsafe(32)
    row = UserSession(user_id=user.id, token_hash=_hash_token(raw), csrf_token=secrets.token_urlsafe(24),
                      expires_at=utcnow() + timedelta(hours=settings.session_ttl_hours))
    db.add(row)
    db.flush()
    keep = [i for (i,) in db.query(UserSession.id).filter_by(user_id=user.id).order_by(UserSession.id.desc()).limit(settings.max_sessions_per_user).all()]
    if keep:
        db.query(UserSession).filter(UserSession.user_id == user.id, UserSession.id.notin_(keep)).delete(synchronize_session=False)
    db.commit()
    return raw, row


def revoke_all_sessions(db: Session, user: User, except_id: int | None = None) -> None:
    q = db.query(UserSession).filter(UserSession.user_id == user.id)
    if except_id is not None:
        q = q.filter(UserSession.id != except_id)
    q.delete(synchronize_session=False)


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
        raise HTTPException(status_code=401, detail={"code": "not_authenticated", "message": "Your session has ended. Please sign in again."})
    try:
        ratelimit.hit("api:user", str(ctx.user.id), settings.rl_api_user_per_min, 60, "You're sending requests too quickly. Please slow down for a moment.")
    except ratelimit.RateLimited as exc:
        raise ratelimit.ensure_http(exc) from exc
    if request.method not in SAFE_METHODS:
        sent = request.headers.get("x-csrf-token", "")
        if not sent or not hmac.compare_digest(sent, ctx.session.csrf_token):
            raise HTTPException(status_code=403, detail={"code": "csrf", "message": "Missing or invalid CSRF token. Reload the page."})
    return ctx


def current_user(ctx: AuthContext = Depends(auth_context)) -> User:
    return ctx.user


def is_admin(db: Session, user: User) -> bool:
    """Admins may open the Pipeline page and the operational endpoints (queue, metrics, service status).
    Configured with ADMIN_EMAILS. In production the address must also be verified (Google vouched for it), so a password
    sign-up with someone else's address cannot become admin. With no list set, only in development the first account is admin."""
    email = normalize_email(user.email)
    listed = settings.admin_email_set
    if listed:
        return email in listed and (user.email_verified or not settings.is_production)
    if settings.is_production:
        return False
    return user.id == db.query(func.min(User.id)).scalar()


def require_admin(user: User = Depends(current_user), db: Session = Depends(get_db)) -> User:
    if not is_admin(db, user):
        raise HTTPException(status_code=403, detail={"code": "admin_only", "message": "Only an administrator can open this."})
    return user
