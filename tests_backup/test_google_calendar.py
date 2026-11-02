import base64
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app import google_calendar
from app.config import settings
from app.crypto import encrypt_token
from app.models import GoogleCalendarConnection, OAuthState, User, utcnow


def _id_token(nonce, email="pat@example.com"):
    claims = {
        "iss": "https://accounts.google.com",
        "aud": settings.google_client_id,
        "exp": time.time() + 300,
        "iat": time.time(),
        "nonce": nonce,
        "sub": "google-user-123",
        "email": email,
        "email_verified": True,
    }
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"header.{payload}.signature"


def test_oauth_state_is_user_bound_and_requests_readonly_access(db, user, monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "client-id")
    monkeypatch.setattr(settings, "google_client_secret", "client-secret")
    monkeypatch.setattr(settings, "public_base_url", "https://contextos.example")
    url = google_calendar.create_state(db, user)
    params = parse_qs(urlparse(url).query)
    row = db.query(OAuthState).filter_by(purpose="google_calendar").one()
    assert params["scope"] == ["openid email " + google_calendar.CALENDAR_SCOPE]
    assert params["redirect_uri"] == ["https://contextos.example/integrations/google-calendar/callback"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["access_type"] == ["offline"]

    other = User(email="other@example.com", name="Other", password_hash="x")
    db.add(other)
    db.commit()
    with pytest.raises(google_calendar.GoogleCalendarError, match="expired"):
        google_calendar._consume_state(db, params["state"][0], other)
    assert google_calendar._consume_state(db, params["state"][0], user).used_at is not None
    with pytest.raises(google_calendar.GoogleCalendarError):
        google_calendar._consume_state(db, params["state"][0], user)


def test_save_calendar_connection_validates_nonce_and_encrypts_tokens(db, user, monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "client-id")
    monkeypatch.setattr(settings, "google_client_secret", "client-secret")
    url = google_calendar.create_state(db, user)
    params = parse_qs(urlparse(url).query)
    row = db.query(OAuthState).filter_by(purpose="google_calendar").one()
    requests = []

    def handler(request):
        requests.append(parse_qs(request.content.decode()))
        return httpx.Response(200, json={
            "access_token": "plain-access-token",
            "refresh_token": "plain-refresh-token",
            "expires_in": 3600,
            "scope": f"openid email {google_calendar.CALENDAR_SCOPE}",
            "id_token": _id_token(row.nonce),
        })

    conn = google_calendar.save_connection(db, user, "oauth-code", params["state"][0], httpx.MockTransport(handler))
    assert conn.email == "pat@example.com"
    assert conn.google_sub == "google-user-123"
    assert conn.access_token_encrypted != "plain-access-token"
    assert conn.refresh_token_encrypted != "plain-refresh-token"
    assert requests[0]["code_verifier"]
    assert conn.status == "connected"
    assert google_calendar.get_connection(db, user.id).id == conn.id
    assert db.query(OAuthState).filter_by(purpose="google_calendar").one().code_verifier is None


def test_events_today_refreshes_expiring_token_and_filters_by_local_day(db, user, monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "client-id")
    monkeypatch.setattr(settings, "google_client_secret", "client-secret")
    fixed_now = datetime(2026, 3, 20, 3, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(google_calendar, "utcnow", lambda: fixed_now)
    conn = GoogleCalendarConnection(
        owner_id=user.id,
        google_sub="google-user-123",
        email=user.email,
        access_token_encrypted=encrypt_token("old-access"),
        refresh_token_encrypted=encrypt_token("refresh-token"),
        access_token_expires_at=fixed_now - timedelta(minutes=5),
        status="connected",
    )
    db.add(conn)
    db.commit()
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "fresh-access", "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer fresh-access"
        if request.url.path.endswith("/calendarList"):
            return httpx.Response(200, json={"items": [{"id": "primary"}, {"id": "work-calendar"}]})
        assert request.url.params["timeMin"] == "2026-03-19T18:30:00Z"
        assert request.url.params["timeMax"] == "2026-03-20T18:30:00Z"
        if request.url.path.endswith("/work-calendar/events"):
            return httpx.Response(200, json={"items": [{"summary": "Work review", "start": {"dateTime": "2026-03-20T05:00:00Z"},
                                                       "end": {"dateTime": "2026-03-20T06:00:00Z"}}]})
        return httpx.Response(200, json={"items": [
            {"summary": "Midnight meeting", "start": {"dateTime": "2026-03-19T18:30:00Z"},
             "end": {"dateTime": "2026-03-19T19:30:00Z"}},
            {"summary": "Overnight meeting", "start": {"dateTime": "2026-03-19T17:00:00Z"},
             "end": {"dateTime": "2026-03-19T19:00:00Z"}},
            {"summary": "All day", "start": {"date": "2026-03-20"}, "end": {"date": "2026-03-21"}},
            {"summary": "Tomorrow", "start": {"dateTime": "2026-03-20T18:30:00Z"},
             "end": {"dateTime": "2026-03-20T19:30:00Z"}},
            {"summary": "Cancelled", "status": "cancelled", "start": {"dateTime": "2026-03-20T09:00:00+05:30"}},
        ]})

    events = google_calendar.events_today(db, user.id, httpx.MockTransport(handler))
    assert events == [
        {"title": "Overnight meeting", "time": "Started before today"},
        {"title": "Midnight meeting", "time": "12:00 AM"},
        {"title": "All day", "time": "All day"},
        {"title": "Work review", "time": "10:30 AM"},
    ]
    assert len(requests) == 4
    assert google_calendar.access_token(db, conn) == "fresh-access"


def test_events_today_returns_none_for_unconnected_user(db, user):
    assert google_calendar.events_today(db, user.id) is None


def test_calendar_connection_status_and_disconnect_are_owner_scoped(db, user, monkeypatch):
    from app.routes_google_calendar import connection_status, disconnect

    other = User(email="other@example.com", name="Other", password_hash="x")
    db.add(other)
    db.flush()
    first = GoogleCalendarConnection(owner_id=user.id, google_sub="first", email=user.email,
                                     access_token_encrypted=encrypt_token("access"),
                                     refresh_token_encrypted=encrypt_token("refresh"))
    second = GoogleCalendarConnection(owner_id=other.id, google_sub="second", email=other.email,
                                      access_token_encrypted=encrypt_token("other-access"),
                                      refresh_token_encrypted=encrypt_token("other-refresh"))
    db.add_all([first, second])
    db.commit()
    monkeypatch.setattr(google_calendar, "revoke", lambda conn: conn.owner_id == user.id)

    assert connection_status(user, db)["connected"]
    response = disconnect(user, db)
    assert response == {"ok": True, "revoked": True}
    assert google_calendar.get_connection(db, user.id) is None
    assert google_calendar.get_connection(db, other.id).id == second.id


def test_assistant_meeting_answer_uses_calendar_without_claiming_unconnected_calendar(db, user, monkeypatch):
    from app import assistant

    monkeypatch.setattr(google_calendar, "events_today", lambda *_args, **_kwargs: [
        {"title": "Planning", "time": "10:00 AM"},
    ])
    answer, sources = assistant._meeting_answer(db, user.id, "Any meeting today?")
    assert "Planning" in answer and "Google Calendar" in answer
    assert "message-based suggestions" not in answer
    assert sources == []


def test_assistant_meeting_answer_discloses_unconnected_calendar(db, user):
    from app.assistant import _meeting_answer

    answer, _sources = _meeting_answer(db, user.id, "Any meetings today?")
    assert "doesn't confirm whether your calendar is empty" in answer
    assert "Connect it in Settings" in answer
