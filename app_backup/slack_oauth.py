"""Slack OAuth v2 (authorization-code flow) and token lifecycle.

Flow: /connect -> create single-use state bound to the logged-in user -> Slack -> /callback validates state,
exchanges the code at oauth.v2.access, stores ENCRYPTED tokens + granted scopes + identity.
"""
import hashlib
import logging
import secrets
from datetime import timedelta
from urllib.parse import urlencode

import httpx
from sqlalchemy.orm import Session

from app.config import ACCESS_LEVELS, DEFAULT_ACCESS_LEVEL, settings
from app.crypto import decrypt_token, encrypt_token
from app.models import OAuthState, SlackConnection, User, as_utc, utcnow
from app.slack_client import SlackAuthError

log = logging.getLogger("contextos.oauth")
AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
TOKEN_URL = "https://slack.com/api/oauth.v2.access"
STATE_TTL = timedelta(minutes=10)


class SlackOAuthError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def _h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def create_state(db: Session, user: User, level: str = DEFAULT_ACCESS_LEVEL) -> str:
    if level not in ACCESS_LEVELS:
        raise SlackOAuthError("bad_level", "Unknown permission level.")
    raw = secrets.token_urlsafe(32)
    db.add(OAuthState(state_hash=_h(raw), user_id=user.id, purpose="slack_connect", payload=level, expires_at=utcnow() + STATE_TTL))
    db.commit()
    return raw


def consume_state(db: Session, raw: str | None, user: User) -> str:
    """Valid only once, only before expiry, only for the user who started the flow. Returns the access level chosen."""
    if not raw:
        raise SlackOAuthError("missing_state")
    row = db.query(OAuthState).filter_by(state_hash=_h(raw), purpose="slack_connect").first()
    if not row or row.used_at is not None or as_utc(row.expires_at) <= utcnow() or row.user_id != user.id:
        raise SlackOAuthError("invalid_state", "OAuth state is invalid, expired, already used, or belongs to another user.")
    row.used_at = utcnow()
    db.commit()
    return row.payload if row.payload in ACCESS_LEVELS else DEFAULT_ACCESS_LEVEL


def build_authorize_url(state: str, level: str = DEFAULT_ACCESS_LEVEL) -> str:
    if not settings.slack_oauth_configured:
        raise SlackOAuthError("not_configured", "Set SLACK_CLIENT_ID and SLACK_CLIENT_SECRET in .env.")
    params = {"client_id": settings.slack_client_id, "user_scope": ",".join(settings.scopes_for_level(level)),
              "redirect_uri": settings.redirect_uri, "state": state}
    if settings.bot_scope_list:
        params["scope"] = ",".join(settings.bot_scope_list)
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def _post_token(data: dict, transport: httpx.BaseTransport | None = None) -> dict:
    payload = {"client_id": settings.slack_client_id, "client_secret": settings.slack_client_secret, **data}
    try:
        with httpx.Client(timeout=20, transport=transport) as client:
            resp = client.post(TOKEN_URL, data=payload)
        body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SlackOAuthError("network", f"Could not reach Slack: {type(exc).__name__}") from exc
    if not body.get("ok"):
        raise SlackOAuthError(body.get("error", "oauth_failed"))
    return body


def exchange_code(code: str, transport: httpx.BaseTransport | None = None) -> dict:
    return _post_token({"code": code, "redirect_uri": settings.redirect_uri}, transport)


def save_connection(db: Session, user: User, payload: dict, level: str = DEFAULT_ACCESS_LEVEL) -> tuple[SlackConnection, bool]:
    """Upsert the (user, workspace) connection from an oauth.v2.access response. Returns (connection, created)."""
    team = payload.get("team") or {}
    authed = payload.get("authed_user") or {}
    team_id, slack_user_id, user_token = team.get("id"), authed.get("id"), authed.get("access_token")
    if not team_id or not slack_user_id:
        raise SlackOAuthError("bad_response", "Slack response did not include workspace/user identity.")
    if not user_token:
        raise SlackOAuthError("no_user_token", "Slack returned no user token. Slack did not grant any user permissions.")
    conn = db.query(SlackConnection).filter_by(owner_id=user.id, team_id=team_id).first()
    created = conn is None
    if created:
        conn = SlackConnection(owner_id=user.id, team_id=team_id, slack_user_id=slack_user_id)
        db.add(conn)
    elif conn.slack_user_id != slack_user_id:
        raise SlackOAuthError("identity_mismatch", "This ContextOS account is already linked to a different Slack user in that workspace.")
    conn.team_name = team.get("name") or conn.team_name
    conn.access_level = level if level in ACCESS_LEVELS else DEFAULT_ACCESS_LEVEL
    conn.user_token_encrypted = encrypt_token(user_token)
    conn.granted_user_scopes = ",".join(sorted(s for s in (authed.get("scope") or "").split(",") if s))
    refresh, expires_in = authed.get("refresh_token"), authed.get("expires_in")
    conn.user_refresh_token_encrypted = encrypt_token(refresh) if refresh else None
    conn.user_token_expires_at = utcnow() + timedelta(seconds=int(expires_in)) if expires_in else None
    if payload.get("access_token"):  # bot token present only when bot scopes were requested
        conn.bot_token_encrypted = encrypt_token(payload["access_token"])
        conn.bot_user_id = payload.get("bot_user_id")
        conn.granted_bot_scopes = ",".join(sorted(s for s in (payload.get("scope") or "").split(",") if s))
    conn.status, conn.revoked_at, conn.last_sync_error = "connected", None, None
    conn.connected_at = utcnow()
    db.commit()
    return conn, created


def refresh_user_token(db: Session, conn: SlackConnection, transport: httpx.BaseTransport | None = None) -> None:
    if not conn.user_refresh_token_encrypted:
        return
    body = _post_token({"grant_type": "refresh_token", "refresh_token": decrypt_token(conn.user_refresh_token_encrypted)}, transport)
    new_token = body.get("access_token") or (body.get("authed_user") or {}).get("access_token")
    if not new_token:
        raise SlackOAuthError("refresh_failed")
    conn.user_token_encrypted = encrypt_token(new_token)
    if body.get("refresh_token"):
        conn.user_refresh_token_encrypted = encrypt_token(body["refresh_token"])
    if body.get("expires_in"):
        conn.user_token_expires_at = utcnow() + timedelta(seconds=int(body["expires_in"]))
    db.commit()


def get_token(db: Session, conn: SlackConnection, kind: str = "user", transport: httpx.BaseTransport | None = None) -> str:
    """Server-side token selection: decrypt the right token, refreshing a rotating user token if needed."""
    if conn.status != "connected":
        raise SlackAuthError("token", "connection_not_active")
    if kind == "bot":
        if not conn.bot_token_encrypted:
            raise SlackAuthError("token", "no_bot_token")
        return decrypt_token(conn.bot_token_encrypted)
    exp = as_utc(conn.user_token_expires_at)
    if exp and exp - utcnow() < timedelta(minutes=5):
        try:
            refresh_user_token(db, conn, transport)
        except SlackOAuthError as exc:
            raise SlackAuthError("oauth.v2.access", "token_expired", {"reason": exc.code}) from exc
    return decrypt_token(conn.user_token_encrypted)


def mark_needs_reauth(db: Session, conn: SlackConnection, reason: str) -> None:
    conn.status, conn.sync_state, conn.last_sync_error = "needs_reauth", "error", f"Slack authorization problem: {reason}"
    db.commit()


def wipe_tokens(conn: SlackConnection) -> None:
    conn.user_token_encrypted = ""
    conn.user_refresh_token_encrypted = None
    conn.bot_token_encrypted = None
    conn.user_token_expires_at = None
