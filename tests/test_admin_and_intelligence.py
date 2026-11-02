"""Admin-only Pipeline page + the features Qdrant and Neo4j drive (with PostgreSQL fallbacks)."""
import hashlib
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import auth, demo, integrations, intelligence
from app.config import Settings, settings
from app.models import MessageProject, ProcessingJob, Project, SlackMessage, User, utcnow

ORIGIN = {"Origin": "http://localhost:8000"}


def _signup(email):
    from app.main import app
    c = TestClient(app, base_url="http://localhost:8000")
    c.__enter__()
    r = c.post("/auth/register", json={"email": email, "password": "correct horse battery"}, headers=ORIGIN)
    assert r.status_code == 200, r.text
    return c, r.json()["user"]


# ------------------------------------------------------------------ admin ---
def test_pipeline_endpoints_are_admin_only(db, monkeypatch):
    monkeypatch.setattr(settings, "admin_emails", "boss@example.com")
    boss, boss_user = _signup("boss@example.com")
    other, other_user = _signup("staff@example.com")
    try:
        assert boss_user["is_admin"] is True and other_user["is_admin"] is False
        assert other.get("/auth/me").json()["user"]["is_admin"] is False
        for path in ("/api/pipeline/overview", "/api/pipeline/events", "/api/selfcheck", "/api/integrations", "/queue/summary", "/queue/jobs",
                     "/api/pipeline/metrics", "/messages/1/journey", "/api/intelligence/status"):
            assert other.get(path).status_code == 403, path
            assert other.get(path).json()["detail"]["code"] == "admin_only"
        assert other.post("/api/intelligence/rebuild").status_code == 403
        assert boss.get("/api/pipeline/overview").status_code == 200
        assert boss.get("/api/selfcheck").status_code == 200
        assert boss.get("/api/intelligence/status").status_code == 200
        # ordinary features stay open to everyone, but never show queue health or engine details to a non-admin
        summary = other.get("/dashboard/summary")
        assert summary.status_code == 200 and "queued_jobs" not in summary.json() and "failed_jobs" not in summary.json()
        assert "queued_jobs" in boss.get("/dashboard/summary").json()
        assert other.get("/api/people").status_code == 200
        # the sign-in screen's public config reveals nothing about the server
        public = other.get("/api/config").json()
        assert "env" not in public and "llm_enabled" not in public
    finally:
        boss.__exit__(None, None, None)
        other.__exit__(None, None, None)


def test_default_admin_is_the_owner_only(db, monkeypatch):
    """Out of the box the one admin is kuch2731@gmail.com. Nobody else, and never 'whoever signed up first'."""
    monkeypatch.undo()  # use the real default from config.py, not a value patched by another test
    assert Settings().admin_email_set == {"kuch2731@gmail.com"}


def test_admin_rules(db, monkeypatch):
    owner = User(email="Kuch2731@Gmail.com", name="Owner", password_hash="x", email_verified=True)
    first = User(email="first@example.com", name="First", password_hash="x", email_verified=True)
    unverified = User(email="boss@example.com", name="Boss", password_hash="x", email_verified=False)
    db.add_all([owner, first, unverified]); db.commit()
    monkeypatch.setattr(settings, "admin_emails", "kuch2731@gmail.com")
    monkeypatch.setattr(settings, "app_env", "production")
    assert auth.is_admin(db, owner)                                     # case-insensitive match on the listed address
    assert not auth.is_admin(db, first)                                 # the first account is NOT special any more
    monkeypatch.setattr(settings, "admin_emails", "kuch2731@gmail.com, boss@example.com")
    assert not auth.is_admin(db, unverified)                            # listed but unverified: refused in production
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "google_client_id", "gid"); monkeypatch.setattr(settings, "google_client_secret", "gsec")
    assert not auth.is_admin(db, unverified)                            # Google is configured, so an unverified address can't be admin even in dev
    monkeypatch.setattr(settings, "google_client_id", ""); monkeypatch.setattr(settings, "google_client_secret", "")
    assert auth.is_admin(db, unverified)                                # local dev with no Google sign-in at all: nothing could be verified
    monkeypatch.setattr(settings, "admin_emails", "")
    assert not auth.is_admin(db, owner) and not auth.is_admin(db, first)  # empty list: nobody


def test_startup_warns_when_qdrant_or_neo4j_missing(monkeypatch):
    monkeypatch.setattr(settings, "qdrant_url", "")
    monkeypatch.setattr(settings, "neo4j_uri", "")
    text = " ".join(settings.production_problems())
    assert "QDRANT_URL" in text and "NEO4J_URI" in text


# ----------------------------------------------------------- fake services ---
qc = pytest.importorskip("qdrant_client")


def fake_embed(texts):
    out = []
    for t in texts:
        v = [0.0] * 384
        for w in t.lower().split():
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 384] += 1.0
        out.append(v)
    return out


@pytest.fixture()
def qdrant(monkeypatch):
    client = qc.QdrantClient(":memory:")
    monkeypatch.setattr(settings, "qdrant_url", "http://memory")
    monkeypatch.setattr(integrations, "_qdrant", lambda: client)
    monkeypatch.setattr(integrations, "_embed", fake_embed)
    monkeypatch.setattr(integrations, "_collection_ready", False)
    return client


def _msg(db, user, conn, text, ts, author="Pat", prio="P2", score=40, **kw):
    m = SlackMessage(owner_id=user.id, connection_id=conn.id, team_id=conn.team_id, channel_id="C1", conv_type="public_channel", slack_ts=ts,
                     text=text, raw_text=text, priority_score=score, priority_label=prio, message_created_at=utcnow() - timedelta(hours=1),
                     author_name=author, user_id=author, **kw)
    db.add(m); db.flush()
    return m


# ------------------------------------------------------------------ qdrant ---
def test_smart_search_fuses_words_and_meaning_and_stays_tenant_scoped(db, user, conn, monkeypatch):
    other = User(email="o@example.com", name="O", password_hash="x"); db.add(other); db.commit()
    mine = _msg(db, user, conn, "Customers cannot sign in after the deploy", "1.1")
    words = _msg(db, user, conn, "Lunch plans for the sign off party", "1.2", prio="P3", score=5)
    theirs = _msg(db, other, conn, "Their private login outage", "1.3")
    db.commit()
    monkeypatch.setattr(settings, "qdrant_url", "http://memory")
    monkeypatch.setattr(integrations, "search_vectors", lambda q, uid, limit=5, **kw: [
        {"message_id": mine.id, "score": 0.71}, {"message_id": theirs.id, "score": 0.99}])   # a poisoned index points at another tenant's row
    out = intelligence.smart_search(db, user, "login problem fix", limit=10)
    ids = {r["id"]: r for r in out["results"]}
    assert mine.id in ids and ids[mine.id]["match"] == "meaning" and ids[mine.id]["similarity"] == 0.71
    assert theirs.id not in ids                                                  # re-loaded by owner: never shown
    kw = intelligence.smart_search(db, user, "sign party", limit=10)
    assert {r["id"] for r in kw["results"]} >= {mine.id, words.id}
    monkeypatch.setattr(settings, "qdrant_url", "")
    assert intelligence.smart_search(db, user, "sign", limit=5)["semantic"] == "off"


def test_related_messages_exclude_self_and_other_tenants(db, user, conn, qdrant):
    other = User(email="o@example.com", name="O", password_hash="x"); db.add(other); db.commit()
    a = _msg(db, user, conn, "phoenix cutover blocked on dns change", "2.1")
    b = _msg(db, user, conn, "dns change for phoenix cutover is still blocked", "2.2")
    c = _msg(db, other, conn, "phoenix cutover blocked on dns change", "2.3")
    db.commit()
    assert integrations.index_vectors([a, b, c]) == "indexed"
    res = intelligence.related_messages(db, user, a, limit=5)
    got = {r["id"] for r in res["related"]}
    assert res["source"] == "qdrant" and b.id in got and a.id not in got and c.id not in got


def test_merge_suggestions_fallback_and_qdrant(db, user, conn, monkeypatch):
    big = Project(owner_id=user.id, name="Phoenix migration", slug="phoenix-migration", status="auto", message_count=9)
    small = Project(owner_id=user.id, name="DB cutover", slug="db-cutover", status="auto", message_count=4)
    db.add_all([big, small]); db.flush()
    for i in range(9):
        m = _msg(db, user, conn, f"cutover step {i}", f"3.{i}")
        db.add(MessageProject(message_id=m.id, project_id=big.id, owner_id=user.id))
        if i < 4:
            db.add(MessageProject(message_id=m.id, project_id=small.id, owner_id=user.id))
    db.commit()
    monkeypatch.setattr(integrations, "similar_project_pairs", lambda uid, s: None)
    out = intelligence.merge_suggestions(db, user)
    assert out["source"] == "postgresql" and out["suggestions"][0]["from"]["id"] == small.id and out["suggestions"][0]["into"]["id"] == big.id
    monkeypatch.setattr(integrations, "similar_project_pairs", lambda uid, s: [(big.id, small.id, 0.91)])
    out = intelligence.merge_suggestions(db, user)
    assert out["source"] == "qdrant" and out["suggestions"][0]["from"]["id"] == small.id


def test_semantic_escalation_is_capped_explained_and_idempotent(db, user, conn, monkeypatch):
    urgent = _msg(db, user, conn, "Production checkout is down, customers cannot pay", "4.1", prio="P0", score=90)
    quiet = _msg(db, user, conn, "people still cannot pay at checkout", "4.2", prio="P3", score=30)
    db.commit()
    monkeypatch.setattr(settings, "qdrant_url", "http://memory")
    monkeypatch.setattr(integrations, "nearest_urgent", lambda texts, uid, s: [(i, urgent.id, 0.9) for i in range(len(texts))])
    r1 = intelligence.apply_semantic_escalation(db, user.id)
    db.refresh(quiet)
    assert r1["raised"] == 1 and quiet.priority_score == 38 and quiet.priority_label == "P2" and "similar to an urgent message" in quiet.reason
    assert intelligence.apply_semantic_escalation(db, user.id)["raised"] == 0     # once only
    db.refresh(urgent)
    assert urgent.priority_label == "P0"                                          # the urgent one is never touched


def test_backfill_embeds_unindexed_messages(db, user, conn, qdrant):
    a = _msg(db, user, conn, "release notes need review", "5.1")
    p3 = _msg(db, user, conn, "lunch?", "5.2", prio="P3", score=2)
    db.commit()
    res = intelligence.backfill(db, user.id)
    db.refresh(a); db.refresh(p3)
    assert res["qdrant"] == "ok" and res["vectors_added"] == 1 and a.indexed_at is not None and p3.indexed_at is None   # P3 never embedded
    assert integrations.qdrant_stats(user.id)["messages"] == 1


def test_worker_schedules_backfill_once_an_hour(db, user, conn, monkeypatch):
    from app import worker
    _msg(db, user, conn, "something important", "6.1"); db.commit()
    monkeypatch.setattr(settings, "qdrant_url", "http://memory")
    worker.schedule_index_backfill(db); worker.schedule_index_backfill(db)
    assert db.query(ProcessingJob).filter_by(job_type="index_backfill", owner_id=user.id).count() == 1
    monkeypatch.setattr(settings, "qdrant_url", "")
    db.query(ProcessingJob).delete(); db.commit()
    worker.schedule_index_backfill(db)
    assert db.query(ProcessingJob).filter_by(job_type="index_backfill").count() == 0   # nothing to backfill into


# ------------------------------------------------------------------- neo4j ---
def test_people_and_risks_postgres_fallback(db, user, monkeypatch):
    monkeypatch.setattr(settings, "neo4j_uri", "")
    demo.seed_demo(db, user, size="full", process=True)
    ov = intelligence.people_overview(db, user)
    assert ov["source"] == "postgresql" and ov["people"]
    names = {p["name"] for p in ov["people"]}
    assert "Priya Nair" in names and not any(n.lower().startswith("you") for n in names)
    d = intelligence.person_detail(db, user, "Priya Nair")
    assert d["projects"] and d["works_with"] and all(w["name"] != "Priya Nair" for w in d["works_with"])


def test_graph_results_are_merged_by_name_and_never_include_you(db, user, conn, monkeypatch):
    _msg(db, user, conn, "my own message", "7.1", author="Me Myself", is_self=True); db.commit()
    monkeypatch.setattr(settings, "neo4j_uri", "bolt://x")
    monkeypatch.setattr(integrations, "graph_people", lambda uid, limit=60: [
        {"name": "Priya Nair", "messages": 5, "projects": 3, "last_active": "2026-10-30T10:00:00"},
        {"name": "priya nair", "messages": 4, "projects": 2, "last_active": "2026-10-31T10:00:00"},     # same person on Gmail
        {"name": "Me Myself", "messages": 9, "projects": 4, "last_active": None}])
    ov = intelligence.people_overview(db, user)
    assert ov["source"] == "neo4j" and [p["name"] for p in ov["people"]] == ["Priya Nair"]
    assert ov["people"][0]["messages"] == 9 and ov["people"][0]["projects"] == 3 and ov["people"][0]["connector"] is True


def test_single_point_of_failure_detected_from_graph(db, user, monkeypatch):
    monkeypatch.setattr(settings, "neo4j_uri", "bolt://x")
    monkeypatch.setattr(integrations, "graph_project_authors", lambda uid: [
        {"id": 1, "project": "Phoenix", "authors": [{"name": "Dev", "n": 14}, {"name": "Ana", "n": 3}]},
        {"id": 2, "project": "Atlas", "authors": [{"name": "Dev", "n": 6}, {"name": "Ana", "n": 6}]},
        {"id": 3, "project": "Tiny", "authors": [{"name": "Dev", "n": 3}]}])
    risks = intelligence.project_risks(db, user)
    assert set(risks) == {1} and risks[1]["person"] == "Dev" and risks[1]["share"] == 0.82


# ------------------------------------------------------------------- routes ---
def test_search_and_people_routes(db, monkeypatch):
    monkeypatch.setattr(settings, "admin_emails", "")
    c, me = _signup("route@example.com")
    try:
        r = c.get("/api/search", params={"q": "anything"})
        assert r.status_code == 200 and r.json()["results"] == []
        assert c.get("/api/search", params={"q": "x", "source": "sms"}).status_code == 400
        assert c.get("/messages/999/related").status_code == 404
        assert c.get("/api/people/detail", params={"name": " "}).status_code == 400
        assert c.get("/api/intelligence/status").status_code == 403       # service status is for the administrator
        monkeypatch.setattr(settings, "admin_emails", "route@example.com")
        st = c.get("/api/intelligence/status").json()
        assert {"qdrant", "neo4j", "features"} <= set(st) and all("url" not in str(v).lower() for v in (st["qdrant"], st["neo4j"]))
        assert c.get("/api/projects/merge-suggestions").json()["suggestions"] == []
    finally:
        c.__exit__(None, None, None)
