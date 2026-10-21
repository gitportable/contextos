import httpx
import pytest

from app import google_auth, ratelimit
from app.models import User


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


def _transport(email_verified=True, email="pat@example.com"):
    def handler(req: httpx.Request):
        if req.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(200, json={"sub": "g-123", "email": email, "email_verified": email_verified, "name": "Pat", "picture": ""})
    return httpx.MockTransport(handler)


def test_google_login_creates_user_once_and_state_is_single_use(db):
    url, state = google_auth.start(db)
    assert "code_challenge" in url and "state=" in url
    user = google_auth.finish(db, "code", state, _transport())
    assert user.email == "pat@example.com" and user.google_sub == "g-123" and user.password_hash == "!"
    with pytest.raises(google_auth.GoogleAuthError):
        google_auth.finish(db, "code", state, _transport())  # replay
    _, s2 = google_auth.start(db)
    assert google_auth.finish(db, "code", s2, _transport()).id == user.id and db.query(User).count() == 1


def test_google_unverified_email_rejected(db):
    _, state = google_auth.start(db)
    with pytest.raises(google_auth.GoogleAuthError) as exc:
        google_auth.finish(db, "code", state, _transport(email_verified=False))
    assert exc.value.code == "email_not_verified"
