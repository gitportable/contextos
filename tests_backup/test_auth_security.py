import base64
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app import auth, google_auth, llm, ratelimit
from app.config import settings

GOOD = ["a@example.com", "first.last+tag@sub.example.co.uk", "A@Example.COM"]
BAD = ["", "plain", "a@b", "a@@b.com", "a b@example.com", "a@exa mple.com", ".a@example.com", "a..b@example.com", "a@example.c0m",
       "a@-example.com", "ä@example.com", "a@exämple.com", "x" * 70 + "@example.com", "a@" + "b" * 64 + ".com"]


@pytest.mark.parametrize("e", GOOD)
def test_email_ok(e):
    assert auth.validate_email(e) == e.strip().lower()


@pytest.mark.parametrize("e", BAD)
def test_email_bad(e):
    with pytest.raises(auth.InvalidEmail):
        auth.validate_email(e)


@pytest.fixture()
def client(db):
    from app.main import app
    with TestClient(app, base_url="http://localhost:8000") as c:
        yield c


H = {"Origin": "http://localhost:8000"}


def _register(client, email="u1@example.com", pw="correct horse battery"):
    r = client.post("/auth/register", json={"email": email, "password": pw, "name": "U"}, headers=H)
    return r


def test_register_login_csrf_and_isolation(client):
    r = _register(client)
    assert r.status_code == 200, r.text
    csrf = r.json()["csrf_token"]
    assert "httponly" in r.headers["set-cookie"].lower()
    assert client.get("/messages").status_code == 200
    # writes need CSRF header
    assert client.post("/projects", json={"name": "Zed"}, headers=H).status_code in (401, 403)
    ok = client.post("/projects", json={"name": "Zed Project"}, headers={**H, "X-CSRF-Token": csrf})
    assert ok.status_code in (200, 201), ok.text
    pid = ok.json()["id"]
    # cross-origin write is refused even with valid cookie + csrf
    bad = client.post("/projects", json={"name": "Evil"}, headers={"Origin": "https://evil.example", "X-CSRF-Token": csrf})
    assert bad.status_code == 403
    # second user cannot see or change the first user's project
    other = TestClient(client.app, base_url="http://localhost:8000")
    r2 = _register(other, "u2@example.com")
    c2 = r2.json()["csrf_token"]
    assert other.get(f"/projects/{pid}").status_code == 404
    assert other.patch(f"/projects/{pid}", json={"name": "Hijacked"}, headers={**H, "X-CSRF-Token": c2}).status_code == 404
    assert [p["id"] for p in other.get("/projects").json()] == []


def test_security_headers_and_no_leak(client):
    r = client.get("/api/config")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "no-store" in client.get("/auth/me").headers["cache-control"]
    big = client.post("/auth/login", content=b"x" * (settings.max_body_bytes + 10), headers={**H, "Content-Type": "application/json"})
    assert big.status_code == 413


def test_bad_email_rejected_by_api(client):
    r = client.post("/auth/register", json={"email": "not-an-email", "password": "correct horse battery"}, headers=H)
    assert r.status_code in (400, 422)
    r = client.post("/auth/login", json={"email": "a b@c.com", "password": "x"}, headers=H)
    assert r.status_code in (400, 422)


def test_login_lockout_and_message(client):
    _register(client, "lock@example.com")
    client.post("/auth/logout", headers={**H, "X-CSRF-Token": "x"})
    c = TestClient(client.app, base_url="http://localhost:8000")
    codes = []
    for _ in range(settings.rl_login_fail_lock_after + 3):
        r = c.post("/auth/login", json={"email": "lock@example.com", "password": "wrong password!"}, headers=H)
        codes.append(r.status_code)
    assert 401 in codes and codes[-1] == 429
    d = r.json()["detail"]
    assert d["retry_after"] > 0 and "try again" in d["message"].lower()
    # even the right password is refused while locked (no oracle)
    assert c.post("/auth/login", json={"email": "lock@example.com", "password": "correct horse battery"}, headers=H).status_code == 429


def test_same_error_for_unknown_user_and_wrong_password(client):
    _register(client, "known@example.com")
    c = TestClient(client.app, base_url="http://localhost:8000")
    a = c.post("/auth/login", json={"email": "known@example.com", "password": "nope nope nope"}, headers=H)
    b = c.post("/auth/login", json={"email": "ghost@example.com", "password": "nope nope nope"}, headers=H)
    assert a.status_code == b.status_code == 401 and a.json()["detail"]["message"] == b.json()["detail"]["message"]


def test_ratelimit_primitive():
    for _ in range(3):
        ratelimit.hit("t", "k", 3, 60)
    with pytest.raises(ratelimit.RateLimited):
        ratelimit.hit("t", "k", 3, 60)


# ---------------------------------------------------------------- Google ---
def _claims(**kw):
    c = {"iss": "https://accounts.google.com", "aud": "gid", "sub": "123", "email": "g@example.com", "email_verified": True,
         "nonce": "N", "exp": time.time() + 600, "iat": time.time(), "name": "G"}
    c.update(kw)
    return c


def test_google_claims(monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "gid")
    assert google_auth.validate_claims(_claims(), "N").email == "g@example.com"
    for bad in (dict(iss="https://evil"), dict(aud="other"), dict(exp=1), dict(nonce="X"), dict(email_verified=False), dict(sub=""), dict(email="nope")):
        with pytest.raises(google_auth.GoogleAuthError):
            google_auth.validate_claims(_claims(**bad), "N")


def test_google_prehijack_wipes_unverified_password(db, monkeypatch):
    from app.models import User
    u = User(email="victim@example.com", name="v", password_hash="scrypt$attacker", email_verified=False)
    db.add(u); db.commit()
    user, created = google_auth.upsert_user(db, google_auth.GoogleIdentity("sub1", "victim@example.com", "V", None))
    assert not created and user.password_hash == "" and user.email_verified and user.google_sub == "sub1"
    with pytest.raises(google_auth.GoogleAuthError):
        google_auth.upsert_user(db, google_auth.GoogleIdentity("sub2", "victim@example.com", "V", None))


# -------------------------------------------------------------------- LLM ---
def test_llm_caps_and_429(db, user, monkeypatch):
    monkeypatch.setattr(settings, "llm_api_key", "k")
    monkeypatch.setattr(settings, "llm_user_daily_cap", 3)
    headers = []
    def handle_ok(request):
        headers.append(request.headers.get("Authorization"))
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})
    ok = httpx.MockTransport(handle_ok)
    assert llm.chat(db, user.id, [{"role": "user", "content": "x"}], transport=ok) == "hi"
    assert headers[0] == "Bearer k"
    rl = httpx.MockTransport(lambda r: httpx.Response(429, headers={"Retry-After": "7"}))
    with pytest.raises(llm.LLMRateLimited) as ei:
        llm.chat(db, user.id, [{"role": "user", "content": "x"}], transport=rl)
    assert ei.value.retry_after >= 7
    llm.chat(db, user.id, [{"role": "user", "content": "x"}], transport=ok)  # the 429 still counted (provider may bill it): this is call 3
    with pytest.raises(llm.LLMRateLimited):
        llm.chat(db, user.id, [{"role": "user", "content": "x"}], transport=ok)


def test_llm_stream_emits_completion_deltas_and_hides_thinking(db, user, monkeypatch):
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    emitted = []
    requests = []
    chunks = [
        {"choices": [{"delta": {"content": "The answer "}}]},
        {"choices": [{"delta": {"content": "<think>private reasoning</think>is 42."}}]},
        "[DONE]",
    ]
    body = "\n\n".join(
        "data: " + (chunk if isinstance(chunk, str) else json.dumps(chunk))
        for chunk in chunks
    ) + "\n\n"

    def respond(request):
        requests.append((request.headers.get("Authorization"), json.loads(request.content).get("stream")))
        return httpx.Response(200, content=body, headers={"Content-Type": "text/event-stream"})

    result = llm.chat_stream(db, user.id, [{"role": "user", "content": "x"}], emitted.append,
                             transport=httpx.MockTransport(respond))
    assert requests == [("Bearer test-key", True)]
    assert "".join(emitted) == result == "The answer is 42."


def test_llm_json_parsing():
    assert llm.parse_json_object('Sure! ```json\n{"a": 1}\n``` bye') == {"a": 1}
    assert llm.parse_json_object("no json") is None
