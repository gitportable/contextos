"""Sign in with Google: OpenID Connect authorization-code flow with PKCE, nonce, and a browser-bound state.

  /auth/google/start   -> creates (state, nonce, PKCE verifier) + a random "binder" cookie; stores only hashes/verifier
                          server-side; redirects to Google.
  /auth/google/callback-> checks the state is unused, unexpired and was issued to THIS browser (binder cookie), swaps the
                          code (+PKCE verifier) for tokens at Google's token endpoint over TLS, validates the ID token
                          claims, then finds/creates/links the ContextOS user.

ID-token note: the token arrives directly from Google's token endpoint over TLS, so OIDC Core 3.1.3.7 lets the client
rely on TLS for authenticity and skip re-checking the JWT signature. We still enforce iss, aud, azp, exp, iat, nonce,
email and email_verified. (If you later accept ID tokens from the browser, verify the signature against Google's JWKS.)
"""
import base64
import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlencode

import httpx
from sqlalchemy.orm import Session

from app import auth
from app.config import settings
from app.models import OAuthState, User, as_utc, utcnow

log = logging.getLogger("contextos.google")
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
ISSUERS = {"https://accounts.google.com", "accounts.google.com"}
STATE_TTL = timedelta(minutes=10)
BINDER_COOKIE = "contextos_oauth_binder"


class GoogleAuthError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


@dataclass
class GoogleIdentity:
    sub: str
    email: str
    name: str
    picture: str | None


def _h(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def start(db: Session) -> tuple[str, str]:
    """Returns (google_authorize_url, binder_cookie_value)."""
    if not settings.google_oauth_configured:
        raise GoogleAuthError("not_configured", "Google sign-in is not configured (set GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET).")
    state, binder, nonce, verifier = (secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(24),
                                      secrets.token_urlsafe(64))
    db.add(OAuthState(state_hash=_h(state), purpose="google_login", binder_hash=_h(binder), nonce=nonce, code_verifier=verifier,
                      expires_at=utcnow() + STATE_TTL))
    db.commit()
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    params = {"client_id": settings.google_client_id, "redirect_uri": settings.google_redirect, "response_type": "code",
              "scope": "openid email profile", "state": state, "nonce": nonce, "code_challenge": challenge,
              "code_challenge_method": "S256", "prompt": "select_account", "access_type": "online"}
    return f"{AUTH_URL}?{urlencode(params)}", binder


def _consume_state(db: Session, state: str | None, binder: str | None) -> OAuthState:
    if not state or not binder:
        raise GoogleAuthError("missing_state", "Sign-in session expired. Please try again.")
    row = db.query(OAuthState).filter_by(state_hash=_h(state), purpose="google_login").first()
    if (not row or row.used_at is not None or as_utc(row.expires_at) <= utcnow()
            or not row.binder_hash or not secrets.compare_digest(row.binder_hash, _h(binder))):
        raise GoogleAuthError("invalid_state", "Sign-in session expired or was started in a different browser. Please try again.")
    row.used_at = utcnow()
    db.commit()
    return row


def _exchange(code: str, verifier: str, transport: httpx.BaseTransport | None) -> dict:
    data = {"code": code, "client_id": settings.google_client_id, "client_secret": settings.google_client_secret,
            "redirect_uri": settings.google_redirect, "grant_type": "authorization_code", "code_verifier": verifier}
    try:
        with httpx.Client(timeout=15, transport=transport) as c:
            resp = c.post(TOKEN_URL, data=data)
        body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise GoogleAuthError("network", "Could not reach Google. Please try again.") from exc
    if resp.status_code != 200 or "id_token" not in body:
        log.warning("google_token_exchange_failed status=%s error=%s", resp.status_code, body.get("error"))
        raise GoogleAuthError("exchange_failed", "Google did not accept the sign-in. Please try again.")
    return body


def _claims(id_token: str) -> dict:
    try:
        payload = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except Exception as exc:
        raise GoogleAuthError("bad_token", "Google returned an unreadable sign-in token.") from exc


def validate_claims(claims: dict, nonce: str, now: float | None = None) -> GoogleIdentity:
    now = now or time.time()
    if claims.get("iss") not in ISSUERS:
        raise GoogleAuthError("bad_issuer")
    aud = claims.get("aud")
    if (aud != settings.google_client_id) if isinstance(aud, str) else (settings.google_client_id not in (aud or [])):
        raise GoogleAuthError("bad_audience")
    if claims.get("azp") and claims["azp"] != settings.google_client_id:
        raise GoogleAuthError("bad_audience")
    if float(claims.get("exp", 0)) <= now - 30 or float(claims.get("iat", 0)) > now + 300:
        raise GoogleAuthError("expired", "Sign-in token expired. Please try again.")
    if not claims.get("nonce") or not secrets.compare_digest(str(claims["nonce"]), nonce or ""):
        raise GoogleAuthError("bad_nonce")
    sub = str(claims.get("sub") or "")
    if not sub or len(sub) > 64:
        raise GoogleAuthError("bad_subject")
    if claims.get("email_verified") not in (True, "true"):
        raise GoogleAuthError("email_unverified", "Your Google email address is not verified, so ContextOS cannot sign you in with it.")
    try:
        email = auth.validate_email(str(claims.get("email") or ""))
    except auth.InvalidEmail as exc:
        raise GoogleAuthError("bad_email", str(exc)) from exc
    name = str(claims.get("name") or email.split("@")[0])[:200]
    pic = claims.get("picture")
    return GoogleIdentity(sub, email, name, pic if isinstance(pic, str) and pic.startswith("https://") and len(pic) <= 500 else None)


def finish(db: Session, code: str | None, state: str | None, binder: str | None,
           transport: httpx.BaseTransport | None = None) -> tuple[User, bool]:
    """Complete the flow. Returns (user, created)."""
    row = _consume_state(db, state, binder)
    if not code:
        raise GoogleAuthError("missing_code")
    verifier, nonce = row.code_verifier or "", row.nonce or ""
    row.code_verifier = None  # single use; do not keep it around
    db.commit()
    ident = validate_claims(_claims(_exchange(code, verifier, transport)["id_token"]), nonce)
    return upsert_user(db, ident)


def upsert_user(db: Session, ident: GoogleIdentity) -> tuple[User, bool]:
    user = db.query(User).filter_by(google_sub=ident.sub).first()
    created = False
    if user is None:
        user = db.query(User).filter_by(email=ident.email).first()
        if user is not None:
            if user.google_sub and user.google_sub != ident.sub:
                raise GoogleAuthError("account_conflict", "That email is already linked to a different Google account.")
            # Pre-hijack defence: if someone registered this address with a password BEFORE its owner proved ownership
            # via Google, that password belongs to an unverified claimant. Wipe it and kill their sessions.
            if user.password_hash and not user.email_verified:
                user.password_hash = ""
                auth.revoke_all_sessions(db, user)
            user.google_sub = ident.sub
        else:
            user = User(email=ident.email, name=ident.name, google_sub=ident.sub, password_hash="", is_active=True)
            db.add(user)
            created = True
    if not user.is_active:
        raise GoogleAuthError("account_disabled", "This account is disabled.")
    user.email_verified = True
    user.avatar_url = ident.picture or user.avatar_url
    if not user.name:
        user.name = ident.name
    db.commit()
    return user, created
