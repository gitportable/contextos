"""'Continue with Google' (OpenID Connect, authorization code + PKCE). Creates or links a ContextOS account by VERIFIED email."""
import base64
import hashlib
import secrets
from datetime import timedelta

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.models import OAuthState, User, as_utc, utcnow
from app.validators import normalize_email, valid_email

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


class GoogleAuthError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _h(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()


def start(db: Session) -> tuple[str, str]:
    if not settings.google_enabled:
        raise GoogleAuthError("google_not_configured")
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)[:96]
    db.add(OAuthState(state_hash=_h(state), user_id=None, purpose="google_login", code_verifier=verifier,
                      expires_at=utcnow() + timedelta(minutes=10)))
    db.commit()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    from urllib.parse import urlencode
    return AUTH_URL + "?" + urlencode({"client_id": settings.google_client_id, "redirect_uri": settings.google_redirect_uri,
                                       "response_type": "code", "scope": "openid email profile", "state": state,
                                       "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "select_account"}), state


def finish(db: Session, code: str | None, state: str | None, transport: httpx.BaseTransport | None = None) -> User:
    if not code or not state:
        raise GoogleAuthError("missing_code")
    row = db.query(OAuthState).filter_by(state_hash=_h(state)).first()
    if not row or row.purpose != "google_login" or row.used_at is not None or as_utc(row.expires_at) <= utcnow():
        raise GoogleAuthError("invalid_state")
    row.used_at = utcnow()
    db.commit()
    try:
        with httpx.Client(timeout=15, transport=transport) as c:
            tok = c.post(TOKEN_URL, data={"code": code, "client_id": settings.google_client_id, "client_secret": settings.google_client_secret,
                                          "redirect_uri": settings.google_redirect_uri, "grant_type": "authorization_code",
                                          "code_verifier": row.code_verifier})
            if tok.status_code != 200 or not tok.json().get("access_token"):
                raise GoogleAuthError("token_exchange_failed")
            info = c.get(USERINFO_URL, headers={"Authorization": f"Bearer {tok.json()['access_token']}"}).json()
    except (httpx.HTTPError, ValueError) as exc:
        raise GoogleAuthError("network") from exc
    email, sub = normalize_email(info.get("email", "")), info.get("sub")
    if not sub or not valid_email(email) or info.get("email_verified") is not True:
        raise GoogleAuthError("email_not_verified")
    user = db.query(User).filter_by(google_sub=sub).first() or db.query(User).filter_by(email=email).first()
    if user is None:
        user = User(email=email, name=(info.get("name") or email.split("@")[0])[:200], password_hash="!", google_sub=sub,
                    avatar_url=(info.get("picture") or "")[:500] or None)
        db.add(user)
    else:
        if not user.is_active:
            raise GoogleAuthError("account_disabled")
        user.google_sub = user.google_sub or sub
        user.avatar_url = user.avatar_url or ((info.get("picture") or "")[:500] or None)
    user.last_login_at = utcnow()
    db.commit()
    return user
