import base64
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app import google_auth, ratelimit
from app.models import OAuthState, User


def test_lockout_doubles_and_clears(db):
    key = "login:pair:a@b.co:1.2.3.4"
    for i in range(4):
        remaining, lock = ratelimit.record_failure(key, 5)
        assert lock == 0 and remaining == 4 - i
    remaining, lock = ratelimit.record_failure(key, 5)
    assert lock == 900 and ratelimit.lock_remaining(key) > 0
    ratelimit.clear_failures(key)
    assert ratelimit.lock_remaining(key) == 0


def test_generic_rate_limit(db):
    results = [ratelimit.hit("api:test", 3, 60)[0] for _ in range(5)]
    assert results == [True, True, True, False, False]


def _id_token(nonce, email_verified=True, email="pat@example.com"):
    claims = {"iss": "https://accounts.google.com", "aud": google_auth.settings.google_client_id,
              "exp": time.time() + 300, "iat": time.time(), "nonce": nonce, "sub": "g-123",
              "email": email, "email_verified": email_verified, "name": "Pat"}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"header.{payload}.signature"


def _transport(nonce, email_verified=True, email="pat@example.com", token_requests=None):
    def handler(req: httpx.Request):
        if req.url.host == "oauth2.googleapis.com":
            if token_requests is not None:
                token_requests.append(parse_qs(req.content.decode()))
            return httpx.Response(200, json={"access_token": "tok", "id_token": _id_token(nonce, email_verified, email)})
        raise AssertionError(f"Unexpected request to {req.url.host}")
    return httpx.MockTransport(handler)


def test_google_login_creates_user_once_and_state_is_single_use(db):
    url, binder = google_auth.start(db)
    params = parse_qs(urlparse(url).query)
    state = params["state"][0]
    row = db.query(OAuthState).filter_by(purpose="google_login").one()
    token_requests = []
    assert "code_challenge" in params
    assert params["redirect_uri"] == [google_auth.settings.google_redirect]
    user, created = google_auth.finish(db, "code", state, binder, _transport(row.nonce, token_requests=token_requests))
    assert created and token_requests[0]["redirect_uri"] == params["redirect_uri"]
    assert user.email == "pat@example.com" and user.google_sub == "g-123" and user.password_hash == ""
    with pytest.raises(google_auth.GoogleAuthError):
        google_auth.finish(db, "code", state, binder, _transport(row.nonce))  # replay
    url2, binder2 = google_auth.start(db)
    state2 = parse_qs(urlparse(url2).query)["state"][0]
    row2 = db.query(OAuthState).filter_by(purpose="google_login").order_by(OAuthState.id.desc()).first()
    signed_in_user, created_again = google_auth.finish(db, "code", state2, binder2, _transport(row2.nonce))
    assert signed_in_user.id == user.id and not created_again and db.query(User).count() == 1


def test_google_redirect_defaults_to_public_callback(db, monkeypatch):
    monkeypatch.setattr(google_auth.settings, "google_redirect_uri", "")
    monkeypatch.setattr(google_auth.settings, "public_base_url", "https://contextos.example")
    url, _ = google_auth.start(db)
    assert parse_qs(urlparse(url).query)["redirect_uri"] == ["https://contextos.example/auth/google/callback"]


def test_google_unverified_email_rejected(db):
    url, binder = google_auth.start(db)
    state = parse_qs(urlparse(url).query)["state"][0]
    row = db.query(OAuthState).filter_by(purpose="google_login").one()
    with pytest.raises(google_auth.GoogleAuthError) as exc:
        google_auth.finish(db, "code", state, binder, _transport(row.nonce, email_verified=False))
    assert exc.value.code == "email_unverified"
