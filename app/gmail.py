"""Gmail as a second message source. A Gmail account is stored as a SlackConnection with provider='gmail' so every later
stage (scoring, projects, summaries, retention, purge, UI) treats an email exactly like a Slack message:

    Gmail thread  -> conversation (channel_id = thread id, name = subject, conv_type = 'email')
    Gmail message -> SlackMessage (slack_ts derived from Gmail's internalDate, user_id = sender address)

Read-only (gmail.readonly): ContextOS never sends, labels or deletes mail. Promotions/social/forums and list mail are not
imported; mail from no-reply/bulk senders is imported as 'bot' (low priority). Tokens are Fernet-encrypted like Slack's.
"""
import base64
import hashlib
import logging
import re
import secrets
from datetime import timedelta
from email.utils import getaddresses, parseaddr
from urllib.parse import urlencode

import httpx
from sqlalchemy.orm import Session

from app import auth, pipeline, projects
from app.config import settings
from app.crypto import TokenCryptoError, decrypt_token, encrypt_token
from app.jobs import PermanentJobError, RescheduleJob
from app.models import OAuthState, SlackConnection, SlackConversation, User, as_utc, utcnow

log = logging.getLogger("contextos.gmail")
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPES = "openid email https://www.googleapis.com/auth/gmail.readonly"
STATE_TTL = timedelta(minutes=10)
QUERY = "-category:promotions -category:social -category:forums -in:spam -in:trash"
MAX_PER_RUN = 400          # messages fetched per sync run (resumes next run)
MAX_BODY = 4000
BULK_SENDER = re.compile(r"(no-?reply|donotreply|do-not-reply|notifications?@|mailer-daemon|bounce|newsletter|updates?@)", re.I)


class GmailError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def _h(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()


def redirect_uri() -> str:
    return f"{settings.public_base_url.rstrip('/')}/integrations/gmail/callback"


# ------------------------------------------------------------------ OAuth ---
def create_state(db: Session, user: User) -> str:
    if not settings.google_oauth_configured:
        raise GmailError("not_configured", "Google is not configured on this server (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET).")
    raw = secrets.token_urlsafe(32)
    db.add(OAuthState(state_hash=_h(raw), user_id=user.id, purpose="gmail_connect", expires_at=utcnow() + STATE_TTL))
    db.commit()
    return raw


def authorize_url(state: str, hint: str | None = None) -> str:
    params = {"client_id": settings.google_client_id, "redirect_uri": redirect_uri(), "response_type": "code", "scope": SCOPES,
              "state": state, "access_type": "offline", "prompt": "consent", "include_granted_scopes": "false"}
    if hint:
        params["login_hint"] = hint
    return f"{AUTH_URL}?{urlencode(params)}"


def consume_state(db: Session, raw: str | None, user: User) -> None:
    row = db.query(OAuthState).filter_by(state_hash=_h(raw or ""), purpose="gmail_connect").first() if raw else None
    if not row or row.used_at is not None or as_utc(row.expires_at) <= utcnow() or row.user_id != user.id:
        raise GmailError("invalid_state", "That connection link expired. Please start again.")
    row.used_at = utcnow()
    db.commit()


def _token_call(data: dict, transport=None) -> dict:
    data = {**data, "client_id": settings.google_client_id, "client_secret": settings.google_client_secret}
    try:
        with httpx.Client(timeout=15, transport=transport) as c:
            resp = c.post(TOKEN_URL, data=data)
        body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise GmailError("network", "Could not reach Google.") from exc
    if resp.status_code != 200:
        raise GmailError("invalid_grant" if body.get("error") == "invalid_grant" else "exchange_failed", "Google rejected the request.")
    return body


def save_connection(db: Session, user: User, code: str, transport=None) -> tuple[SlackConnection, bool]:
    from app.google_auth import _claims
    body = _token_call({"code": code, "grant_type": "authorization_code", "redirect_uri": redirect_uri()}, transport)
    if "https://www.googleapis.com/auth/gmail.readonly" not in (body.get("scope") or ""):
        raise GmailError("scope_denied", "Gmail permission was not granted.")
    try:
        email = auth.validate_email(str(_claims(body["id_token"]).get("email") or ""))
    except (auth.InvalidEmail, KeyError, Exception) as exc:
        raise GmailError("bad_email", "Google did not return a usable email address.") from exc
    if not body.get("refresh_token"):
        raise GmailError("no_refresh_token", "Google did not return an offline token. Remove ContextOS at myaccount.google.com/permissions and connect again.")
    team = f"gmail:{email}"
    conn = db.query(SlackConnection).filter_by(owner_id=user.id, team_id=team).first()
    created = conn is None
    if created:
        conn = SlackConnection(owner_id=user.id, team_id=team, slack_user_id=email, provider="gmail", access_level="essential")
        db.add(conn)
    conn.team_name = email
    conn.user_token_encrypted = encrypt_token(body["access_token"])
    conn.user_refresh_token_encrypted = encrypt_token(body["refresh_token"])
    conn.user_token_expires_at = utcnow() + timedelta(seconds=int(body.get("expires_in", 3600)))
    conn.granted_user_scopes = "gmail.readonly"
    conn.status, conn.revoked_at, conn.last_sync_error = "connected", None, None
    conn.connected_at = utcnow()
    db.commit()
    return conn, created


def access_token(db: Session, conn: SlackConnection, transport=None) -> str:
    exp = as_utc(conn.user_token_expires_at)
    if exp is None or exp - utcnow() < timedelta(minutes=2):
        try:
            body = _token_call({"grant_type": "refresh_token", "refresh_token": decrypt_token(conn.user_refresh_token_encrypted or "")}, transport)
        except (GmailError, TokenCryptoError) as exc:
            raise GmailError("reauth", "Gmail access expired or was revoked.") from exc
        conn.user_token_encrypted = encrypt_token(body["access_token"])
        conn.user_token_expires_at = utcnow() + timedelta(seconds=int(body.get("expires_in", 3600)))
        db.commit()
    return decrypt_token(conn.user_token_encrypted)


def revoke(db: Session, conn: SlackConnection) -> bool:
    try:
        tok = decrypt_token(conn.user_refresh_token_encrypted or conn.user_token_encrypted)
        with httpx.Client(timeout=8) as c:
            return c.post("https://oauth2.googleapis.com/revoke", data={"token": tok}).status_code == 200
    except Exception:
        return False


# -------------------------------------------------------------- parsing ---
def _b64(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
    except Exception:
        return ""


def _body(payload: dict) -> str:
    """Prefer text/plain; fall back to tag-stripped text/html. Quoted replies and signatures are trimmed."""
    plain, html = [], []

    def walk(part: dict, depth=0):
        if depth > 6:
            return
        mt, data = part.get("mimeType", ""), (part.get("body") or {}).get("data")
        if data and mt == "text/plain":
            plain.append(_b64(data))
        elif data and mt == "text/html":
            html.append(_b64(data))
        for sub in part.get("parts") or []:
            walk(sub, depth + 1)

    walk(payload)
    text = "\n".join(plain) or re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style).*?</\1>", " ", "\n".join(html)))
    lines = []
    for ln in text.splitlines():
        if ln.startswith(">") or re.match(r"^On .+ wrote:$", ln.strip()) or ln.strip() == "--":
            break
        lines.append(ln)
    return re.sub(r"\s+", " ", " ".join(lines)).strip()[:MAX_BODY]


def to_raw(msg: dict, me: str) -> dict | None:
    """Gmail API message (format=full) -> the dict ingest_message() understands. None = skip."""
    h = {x["name"].lower(): x["value"] for x in (msg.get("payload") or {}).get("headers", []) if "name" in x}
    name, sender = parseaddr(h.get("from", ""))
    sender = sender.lower()
    if not sender:
        return None
    tos = {a.lower() for _, a in getaddresses([h.get("to", "")]) if a}
    ccs = {a.lower() for _, a in getaddresses([h.get("cc", "")]) if a}
    bulk = "list-unsubscribe" in h or h.get("precedence", "").lower() in ("bulk", "list", "junk") or bool(BULK_SENDER.search(sender))
    body = _body(msg.get("payload") or {}) or (msg.get("snippet") or "")
    subject = (h.get("subject") or "(no subject)").strip()[:200]
    text = f"{subject}. {body}".strip()
    ms = int(msg.get("internalDate") or 0)
    ts = f"{ms // 1000}.{(ms % 1000) * 1000 + int(hashlib.md5(msg['id'].encode()).hexdigest(), 16) % 1000:06d}"
    return {"ts": ts, "user": sender, "username": (name or sender)[:200] if bulk else None, "bot_id": "bulk" if bulk else None,
            "text": text, "thread_ts": None, "_direct": (me in tos) and not bulk, "_cc": me in ccs, "_subject": subject,
            "_thread": msg.get("threadId") or msg["id"], "_name": name or sender}


class GmailClient:
    def __init__(self, token: str, transport=None):
        self.c = httpx.Client(timeout=20, transport=transport, headers={"Authorization": f"Bearer {token}"})

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.c.close()

    def get(self, path: str, **params) -> dict:
        try:
            r = self.c.get(f"{API}/{path}", params=params)
        except httpx.HTTPError as exc:
            raise GmailError("network", type(exc).__name__) from exc
        if r.status_code == 401:
            raise GmailError("reauth", "Gmail rejected the token.")
        if r.status_code == 403 and "rateLimit" in r.text:
            raise RescheduleJob(60, "gmail rate limited")
        if r.status_code == 429:
            raise RescheduleJob(int(r.headers.get("Retry-After", "60") or 60), "gmail rate limited")
        if r.status_code != 200:
            raise GmailError(f"http_{r.status_code}", "Gmail API error")
        return r.json()


# ----------------------------------------------------------------- sync ---
def sync_connection(db: Session, conn: SlackConnection, mode: str = "incremental", *, on_progress=None, transport=None) -> dict:
    tick = on_progress or (lambda: None)
    first = conn.last_sync_finished_at is None or mode == "initial"
    started = utcnow()
    conn.sync_state, conn.last_sync_started_at, conn.last_sync_error = "running", started, None
    db.commit()
    since = (started - timedelta(days=settings.slack_initial_sync_days)) if first else (as_utc(conn.last_sync_finished_at) - timedelta(hours=6))
    q = f"{QUERY} after:{int(since.timestamp())}"
    new = 0
    try:
        with GmailClient(access_token(db, conn, transport), transport) as g:
            ids, page = [], None
            while len(ids) < MAX_PER_RUN:
                params = {"q": q, "maxResults": 100}
                if page:
                    params["pageToken"] = page
                res = g.get("messages", **params)
                ids += [m["id"] for m in res.get("messages", [])]
                page = res.get("nextPageToken")
                if not page:
                    break
            conn.sync_total_conversations, conn.sync_done_conversations = len(ids), 0
            rows = []
            for i, mid in enumerate(ids[:MAX_PER_RUN]):
                try:
                    raw = to_raw(g.get(f"messages/{mid}", format="full"), conn.slack_user_id)
                except GmailError as exc:
                    if exc.code != "http_404":  # deleted between list and get: skip it, anything else is a real failure
                        raise
                    continue
                except (ValueError, KeyError, TypeError):
                    log.warning("gmail_unparseable_message conn=%s", conn.id)
                    continue
                if raw:
                    thread = raw["_thread"]
                    res = pipeline.ingest_message(db, conn, thread, raw, conv_type="email", source="history")
                    conv = db.query(SlackConversation).filter_by(connection_id=conn.id, channel_id=thread).first()
                    if conv is not None and not conv.name:
                        conv.name = raw["_subject"]
                    if res.action == "created":
                        new += 1
                        rows.append(res.message)
                conn.sync_done_conversations = i + 1
                if (i + 1) % 25 == 0:
                    db.commit()
                    tick()
            conn.messages_synced = (conn.messages_synced or 0) + new
            db.commit()
            try:  # after the source-of-truth commit: failures here never lose mail
                if rows:
                    trace = f"gmail:{conn.id}"
                    pipeline.log_batch(db, conn, rows, fetched=len(ids), trace_id=trace, label="Gmail sync")
                    db.commit()
                    pipeline.link_and_log(db, rows, trace_id=trace)
                    pipeline.index_rows(db, rows, trace)
            except Exception:
                db.rollback()
                log.exception("gmail_after_sync_failed conn=%s", conn.id)
        conn.sync_state, conn.last_sync_finished_at = "idle", started
        if new or first:
            projects.schedule_discovery(db, conn.owner_id, immediate=first)
        db.commit()
        return {"provider": "gmail", "fetched": len(ids), "new_messages": new}
    except GmailError as exc:
        db.rollback()
        if exc.code == "reauth":
            conn.status, conn.sync_state, conn.last_sync_error = "needs_reauth", "error", "Gmail access expired or was revoked. Reconnect Gmail."
            db.commit()
            raise PermanentJobError("Gmail authorization failed") from exc
        conn.sync_state, conn.last_sync_error = "error", f"Gmail: {exc.code}"
        db.commit()
        raise
    except RescheduleJob:
        db.rollback()
        conn.sync_state = "rate_limited"
        db.commit()
        raise
