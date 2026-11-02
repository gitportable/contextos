import base64
import json

import httpx
import pytest

from app import demo, gmail
from app.config import settings
from app.models import MessageProject, Project, SlackConnection, SlackMessage

ME = "me@gmail.com"


def _msg(mid, frm, subject, body, to=ME, extra=None, ms=1_760_000_000_000):
    h = [{"name": "From", "value": frm}, {"name": "To", "value": to}, {"name": "Subject", "value": subject}] + (extra or [])
    return {"id": mid, "threadId": "t" + mid, "internalDate": str(ms),
            "payload": {"mimeType": "text/plain", "headers": h, "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()}}}


def test_to_raw_direct_vs_bulk_and_quote_trimming():
    r = gmail.to_raw(_msg("1", "Priya <p@x.test>", "Phoenix cutover", "Please confirm by 5pm.\n> old quoted text\nOn Mon, X wrote:"), ME)
    assert r["_direct"] and not r["bot_id"] and "old quoted" not in r["text"] and r["user"] == "p@x.test"
    b = gmail.to_raw(_msg("2", "News <newsletter@n.test>", "Digest", "hi", extra=[{"name": "List-Unsubscribe", "value": "<x>"}]), ME)
    assert b["bot_id"] and not b["_direct"]
    assert gmail.to_raw({"id": "3", "payload": {"headers": []}}, ME) is None
    a, c = gmail.to_raw(_msg("a", "A <a@x.test>", "s", "b"), ME), gmail.to_raw(_msg("b", "A <a@x.test>", "s", "b"), ME)
    assert a["ts"] != c["ts"]  # same millisecond, different message ids -> distinct slack_ts


def test_oauth_url_and_state_binding(db, user, monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "gid"); monkeypatch.setattr(settings, "google_client_secret", "gs")
    st = gmail.create_state(db, user)
    url = gmail.authorize_url(st)
    assert "gmail.readonly" in url and "access_type=offline" in url
    from app.models import User
    other = User(email="o@example.com", name="o", password_hash="x"); db.add(other); db.commit()
    with pytest.raises(gmail.GmailError):
        gmail.consume_state(db, st, other)
    gmail.consume_state(db, st, user)
    with pytest.raises(gmail.GmailError):  # single use
        gmail.consume_state(db, st, user)


def test_save_connection_and_sync_with_mock_google(db, user, monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "gid"); monkeypatch.setattr(settings, "google_client_secret", "gs")
    idt = base64.urlsafe_b64encode(json.dumps({"email": ME}).encode()).decode().rstrip("=")
    mails = {f"m{i}": _msg(f"m{i}", "Priya <p@x.test>", "Phoenix cutover plan", f"Please review the Phoenix cutover plan item {i} by Friday.", ms=1_760_000_000_000 + i * 86_400_000) for i in range(8)}

    def handler(req: httpx.Request):
        u = str(req.url)
        if "oauth2.googleapis.com/token" in u:
            return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600, "id_token": "x." + idt + ".y",
                                             "scope": "openid email https://www.googleapis.com/auth/gmail.readonly"})
        if u.endswith("/messages") or "/messages?" in u:
            return httpx.Response(200, json={"messages": [{"id": k} for k in mails]})
        mid = u.split("/messages/")[1].split("?")[0]
        return httpx.Response(200, json=mails[mid])

    t = httpx.MockTransport(handler)
    conn, created = gmail.save_connection(db, user, "code", t)
    assert created and conn.provider == "gmail" and conn.team_id == f"gmail:{ME}" and conn.user_token_encrypted != "at"
    monkeypatch.setattr(settings, "slack_initial_sync_days", 100000)
    out = gmail.sync_connection(db, conn, "initial", transport=t)
    assert out["new_messages"] == 8
    again = gmail.sync_connection(db, conn, "incremental", transport=t)
    assert again["new_messages"] == 0  # idempotent
    row = db.query(SlackMessage).filter_by(connection_id=conn.id).first()
    assert row.conv_type == "email" and row.mentions_me and row.owner_id == user.id


def test_refresh_failure_marks_reauth(db, user, monkeypatch):
    from app.crypto import encrypt_token
    from app.jobs import PermanentJobError
    monkeypatch.setattr(settings, "google_client_id", "gid"); monkeypatch.setattr(settings, "google_client_secret", "gs")
    c = SlackConnection(owner_id=user.id, team_id="gmail:x@gmail.com", slack_user_id="x@gmail.com", provider="gmail", user_token_encrypted=encrypt_token("a"),
                        user_refresh_token_encrypted=encrypt_token("r"), granted_user_scopes="gmail.readonly")
    db.add(c); db.commit()
    t = httpx.MockTransport(lambda r: httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(PermanentJobError):
        gmail.sync_connection(db, c, "initial", transport=t)
    assert c.status == "needs_reauth"


def test_projects_span_slack_and_gmail(db, user):
    demo.seed_demo(db, user, size="full", process=True)
    providers = {p for (p,) in db.query(SlackConnection.provider).filter_by(owner_id=user.id)}
    assert providers == {"slack", "gmail"}
    ph = db.query(Project).filter(Project.name.ilike("phoenix%")).first()
    prov = {p for (p,) in db.query(SlackConnection.provider).join(SlackMessage, SlackMessage.connection_id == SlackConnection.id)
            .join(MessageProject, MessageProject.message_id == SlackMessage.id).filter(MessageProject.project_id == ph.id)}
    assert prov == {"slack", "gmail"}
    bulk = db.query(SlackMessage).filter(SlackMessage.conv_type == "email", SlackMessage.is_bot.is_(True)).all()
    assert bulk and all(m.priority_label in ("P2", "P3") for m in bulk)


def test_sync_skips_deleted_and_malformed_messages(db, user, monkeypatch):
    from app.crypto import encrypt_token
    from app.models import utcnow
    from datetime import timedelta
    monkeypatch.setattr(settings, "google_client_id", "gid"); monkeypatch.setattr(settings, "google_client_secret", "gs")
    c = SlackConnection(owner_id=user.id, team_id="gmail:me@gmail.com", slack_user_id=ME, provider="gmail", user_token_encrypted=encrypt_token("a"),
                        user_refresh_token_encrypted=encrypt_token("r"), user_token_expires_at=utcnow() + timedelta(hours=1), granted_user_scopes="gmail.readonly")
    db.add(c); db.commit()
    good = _msg("ok", "P <p@x.test>", "Phoenix", "Please review by Friday.", ms=int(utcnow().timestamp() * 1000))
    bad = dict(_msg("bad", "P <p@x.test>", "x", "y"), internalDate="not-a-number")

    def handler(req):
        u = str(req.url)
        if "/messages?" in u or u.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": "gone"}, {"id": "bad"}, {"id": "ok"}]})
        mid = u.split("/messages/")[1].split("?")[0]
        return httpx.Response(404, json={}) if mid == "gone" else httpx.Response(200, json={"ok": good, "bad": bad}[mid])

    out = gmail.sync_connection(db, c, "initial", transport=httpx.MockTransport(handler))
    assert out["new_messages"] == 1
