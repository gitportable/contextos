"""Per-user, read-only Google Calendar connection and today's event lookup."""
import base64
import hashlib
import logging
import secrets
from datetime import datetime, time, timedelta, timezone
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy.orm import Session

from app import google_auth
from app.config import settings
from app.crypto import TokenCryptoError, decrypt_token, encrypt_token
from app.models import GoogleCalendarConnection, OAuthState, User, as_utc, utcnow
from app.priority import get_tz

log = logging.getLogger("contextos.google_calendar")
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CALENDAR_LIST_URL = "https://www.googleapis.com/calendar/v3/users/me/calendarList"
USERINFO_SCOPE = "openid email"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
OAUTH_PURPOSE = "google_calendar"
STATE_TTL = timedelta(minutes=10)


class GoogleCalendarError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def redirect_uri() -> str:
    return settings.google_calendar_redirect or f"{settings.public_base_url.rstrip('/')}/integrations/google-calendar/callback"


def create_state(db: Session, user: User) -> str:
    if not settings.google_oauth_configured:
        raise GoogleCalendarError("not_configured", "Google Calendar connection is not configured on this server.")
    if not settings.token_encryption_key.strip():
        raise GoogleCalendarError("encryption_not_configured", "Google Calendar token encryption is not configured on this server.")
    try:
        encrypt_token("calendar-oauth-check")
    except TokenCryptoError as exc:
        raise GoogleCalendarError("encryption_not_configured", "Google Calendar token encryption is not configured correctly on this server.") from exc
    state, nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(24), secrets.token_urlsafe(64)
    db.add(OAuthState(state_hash=_hash(state), user_id=user.id, purpose=OAUTH_PURPOSE, nonce=nonce, code_verifier=verifier,
                      expires_at=utcnow() + STATE_TTL))
    db.commit()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": f"{USERINFO_SCOPE} {CALENDAR_SCOPE}",
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "false",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def _consume_state(db: Session, raw: str | None, user: User) -> OAuthState:
    row = db.query(OAuthState).filter_by(state_hash=_hash(raw or ""), purpose=OAUTH_PURPOSE).first() if raw else None
    if (not row or row.used_at is not None or as_utc(row.expires_at) <= utcnow()
            or row.user_id != user.id):
        raise GoogleCalendarError("invalid_state", "That Google Calendar connection link expired. Please start again.")
    row.used_at = utcnow()
    db.commit()
    return row


def _token_call(data: dict, transport: httpx.BaseTransport | None = None) -> dict:
    payload = {**data, "client_id": settings.google_client_id, "client_secret": settings.google_client_secret}
    try:
        with httpx.Client(timeout=15, transport=transport) as client:
            response = client.post(TOKEN_URL, data=payload)
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise GoogleCalendarError("network", "Could not reach Google. Please try again.") from exc
    if not isinstance(body, dict):
        raise GoogleCalendarError("invalid_response", "Google returned an unreadable token response.")
    if response.status_code != 200:
        code = body.get("error")
        log.warning("google_calendar_token_failed status=%s error=%s", response.status_code, code)
        if code == "invalid_grant":
            raise GoogleCalendarError("invalid_grant", "Google access was revoked. Reconnect your Calendar in Settings.")
        raise GoogleCalendarError("exchange_failed", "Google rejected the Calendar connection request.")
    return body


def save_connection(db: Session, user: User, code: str, raw_state: str,
                    transport: httpx.BaseTransport | None = None) -> GoogleCalendarConnection:
    row = _consume_state(db, raw_state, user)
    verifier, nonce = row.code_verifier or "", row.nonce or ""
    row.code_verifier = None
    db.commit()
    body = _token_call({
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri(),
        "code_verifier": verifier,
    }, transport)
    raw_scopes = body.get("scope")
    granted_scopes = set(raw_scopes.split()) if isinstance(raw_scopes, str) else set()
    if CALENDAR_SCOPE not in granted_scopes:
        raise GoogleCalendarError("scope_denied", "Read-only Google Calendar permission was not granted.")
    if (not isinstance(body.get("access_token"), str) or not body["access_token"].strip()
            or not isinstance(body.get("id_token"), str) or not body["id_token"].strip()):
        raise GoogleCalendarError("invalid_response", "Google did not return the required Calendar credentials.")
    try:
        claims = google_auth._claims(body["id_token"])
        identity = google_auth.validate_claims(claims, nonce)
    except (KeyError, TypeError, ValueError, google_auth.GoogleAuthError) as exc:
        raise GoogleCalendarError("bad_identity", "Google did not return a valid account identity.") from exc

    try:
        expires_in = int(body.get("expires_in", 3600))
    except (TypeError, ValueError) as exc:
        raise GoogleCalendarError("invalid_response", "Google returned an invalid token expiry.") from exc
    if expires_in <= 0:
        raise GoogleCalendarError("invalid_response", "Google returned an invalid token expiry.")
    conn = db.query(GoogleCalendarConnection).filter_by(owner_id=user.id).first()
    refresh_token = body.get("refresh_token")
    if refresh_token is not None and not isinstance(refresh_token, str):
        raise GoogleCalendarError("invalid_response", "Google returned an invalid refresh token.")
    if refresh_token is not None and not refresh_token.strip():
        refresh_token = None
    if not refresh_token and (conn is None or not conn.refresh_token_encrypted or conn.google_sub != identity.sub):
        raise GoogleCalendarError("no_refresh_token", "Google did not return an offline token. Remove ContextOS from your Google account permissions and connect again.")
    if conn and conn.google_sub != identity.sub:
        revoke(conn)
    if conn is None:
        conn = GoogleCalendarConnection(owner_id=user.id, google_sub=identity.sub, email=identity.email)
        db.add(conn)
    conn.google_sub = identity.sub
    conn.email = identity.email
    conn.access_token_encrypted = encrypt_token(body["access_token"])
    if refresh_token:
        conn.refresh_token_encrypted = encrypt_token(refresh_token)
    conn.access_token_expires_at = utcnow() + timedelta(seconds=expires_in)
    conn.status = "connected"
    conn.connected_at = utcnow()
    db.commit()
    return conn


def get_connection(db: Session, owner_id: int) -> GoogleCalendarConnection | None:
    return db.query(GoogleCalendarConnection).filter_by(owner_id=owner_id).first()


def access_token(db: Session, conn: GoogleCalendarConnection, transport: httpx.BaseTransport | None = None) -> str:
    expires_at = as_utc(conn.access_token_expires_at)
    if expires_at is None or expires_at - utcnow() < timedelta(minutes=2):
        try:
            refresh = decrypt_token(conn.refresh_token_encrypted)
        except TokenCryptoError as exc:
            raise GoogleCalendarError("reauth", "Google Calendar access expired. Disconnect and reconnect your account.") from exc
        try:
            body = _token_call({"grant_type": "refresh_token", "refresh_token": refresh}, transport)
        except GoogleCalendarError as exc:
            if exc.code == "invalid_grant":
                conn.status = "needs_reauth"
                db.commit()
                raise GoogleCalendarError("reauth", "Google Calendar access expired or was revoked. Reconnect your account in Settings.") from exc
            raise
        if not isinstance(body.get("access_token"), str) or not body["access_token"].strip():
            raise GoogleCalendarError("invalid_response", "Google did not return a refreshed Calendar token.")
        try:
            expires_in = int(body.get("expires_in", 3600))
        except (TypeError, ValueError) as exc:
            raise GoogleCalendarError("invalid_response", "Google returned an invalid token expiry.") from exc
        if expires_in <= 0:
            raise GoogleCalendarError("invalid_response", "Google returned an invalid token expiry.")
        conn.access_token_encrypted = encrypt_token(body["access_token"])
        conn.access_token_expires_at = utcnow() + timedelta(seconds=expires_in)
        db.commit()
    try:
        return decrypt_token(conn.access_token_encrypted)
    except TokenCryptoError as exc:
        raise GoogleCalendarError("reauth", "Google Calendar access expired. Disconnect and reconnect your account.") from exc


def events_today(db: Session, owner_id: int, transport: httpx.BaseTransport | None = None) -> list[dict] | None:
    """Return local-today events, or None when the user has not connected a calendar."""
    conn = get_connection(db, owner_id)
    if not conn or conn.status != "connected":
        return None

    tz = get_tz()
    now = utcnow().astimezone(tz)
    day_start = datetime.combine(now.date(), datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
    day_end = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
    event_params = {
        "timeMin": day_start.isoformat().replace("+00:00", "Z"),
        "timeMax": day_end.isoformat().replace("+00:00", "Z"),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": 50,
    }
    token = access_token(db, conn, transport)
    events = []
    headers = {"Authorization": f"Bearer {token}"}

    def get_json(client: httpx.Client, url: str, params: dict) -> dict:
        response = client.get(url, params=params, headers=headers)
        if response.status_code == 401:
            conn.status = "needs_reauth"
            db.commit()
            raise GoogleCalendarError("reauth", "Google Calendar access was revoked. Reconnect your account in Settings.")
        if response.status_code != 200:
            log.warning("google_calendar_request_failed status=%s owner=%s", response.status_code, owner_id)
            raise GoogleCalendarError("calendar_unavailable", "Google Calendar could not be reached right now.")
        try:
            data = response.json()
        except ValueError as exc:
            raise GoogleCalendarError("invalid_response", "Google Calendar returned an unreadable response.") from exc
        if not isinstance(data, dict):
            raise GoogleCalendarError("invalid_response", "Google Calendar returned an unreadable response.")
        return data

    try:
        with httpx.Client(timeout=15, transport=transport) as client:
            calendars = []
            calendar_params = {"maxResults": 250}
            while True:
                data = get_json(client, CALENDAR_LIST_URL, calendar_params)
                items = data.get("items") or []
                if not isinstance(items, list):
                    raise GoogleCalendarError("invalid_response", "Google Calendar returned an unreadable calendar list.")
                calendars.extend(item for item in items if isinstance(item, dict) and isinstance(item.get("id"), str))
                next_page = data.get("nextPageToken")
                if not next_page:
                    break
                calendar_params["pageToken"] = next_page
            if not calendars:
                calendars = [{"id": "primary"}]
            for calendar in calendars:
                params = dict(event_params)
                while True:
                    url = f"https://www.googleapis.com/calendar/v3/calendars/{quote(calendar['id'], safe='')}/events"
                    data = get_json(client, url, params)
                    items = data.get("items") or []
                    if not isinstance(items, list):
                        raise GoogleCalendarError("invalid_response", "Google Calendar returned an unreadable event list.")
                    events.extend(event for event in items if isinstance(event, dict) and event.get("status") != "cancelled")
                    next_page = data.get("nextPageToken")
                    if not next_page:
                        break
                    params["pageToken"] = next_page
    except (httpx.HTTPError, ValueError) as exc:
        raise GoogleCalendarError("network", "Google Calendar could not be reached right now.") from exc

    result = []
    for event in events:
        start = event.get("start") or {}
        if not isinstance(start, dict):
            continue
        start_value = start.get("dateTime") or start.get("date")
        if not isinstance(start_value, str) or not start_value:
            continue
        try:
            if "T" in start_value:
                parsed_start = datetime.fromisoformat(start_value.replace("Z", "+00:00"))
                if parsed_start.tzinfo is None:
                    parsed_start = parsed_start.replace(tzinfo=ZoneInfo(start.get("timeZone") or str(tz)))
                parsed_start = parsed_start.astimezone(tz)
                end = event.get("end") or {}
                if not isinstance(end, dict):
                    continue
                end_value = end.get("dateTime")
                if end_value and not isinstance(end_value, str):
                    continue
                if end_value:
                    parsed_end = datetime.fromisoformat(end_value.replace("Z", "+00:00"))
                    if parsed_end.tzinfo is None:
                        parsed_end = parsed_end.replace(tzinfo=ZoneInfo(end.get("timeZone") or str(tz)))
                    parsed_end = parsed_end.astimezone(tz)
                else:
                    parsed_end = parsed_start
                if parsed_start >= day_end.astimezone(tz) or parsed_end <= day_start.astimezone(tz):
                    continue
                time_text = (parsed_start.strftime("%I:%M %p").lstrip("0")
                             if parsed_start.date() == now.date() else "Started before today")
                sort_at = parsed_start
            else:
                date_value = datetime.fromisoformat(start_value).date()
                end = event.get("end") or {}
                if not isinstance(end, dict):
                    continue
                end_date = datetime.fromisoformat(end.get("date", start_value)).date()
                if date_value >= now.date() + timedelta(days=1) or end_date <= now.date():
                    continue
                time_text = "All day"
                sort_at = datetime.combine(date_value, time.min, tzinfo=tz)
        except (ValueError, KeyError, TypeError):
            continue
        title = event.get("summary")
        result.append((sort_at, {"title": title[:300] if isinstance(title, str) and title else "Untitled event", "time": time_text}))
    result.sort(key=lambda event: event[0])
    return [event for _, event in result]


def revoke(conn: GoogleCalendarConnection) -> bool:
    try:
        token = decrypt_token(conn.refresh_token_encrypted or conn.access_token_encrypted)
    except TokenCryptoError:
        return False
    try:
        with httpx.Client(timeout=8) as client:
            return client.post("https://oauth2.googleapis.com/revoke", data={"token": token}).status_code == 200
    except httpx.HTTPError:
        log.info("google_calendar_revoke_failed connection=%s", conn.id)
        return False
