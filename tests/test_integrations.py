import hashlib
from types import SimpleNamespace

import pytest

from app import demo, integrations
from app.config import settings
from app.models import SlackMessage, User

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


def test_vector_search_is_tenant_and_project_scoped(db, user, qdrant):
    other = User(email="o@example.com", name="O", password_hash="x"); db.add(other); db.commit()
    demo.seed_demo(db, user, size="small", process=True)
    mine = db.query(SlackMessage).filter_by(owner_id=user.id).limit(20).all()
    res = integrations.index_messages(mine)
    assert res["vector_ok"], res
    theirs = [SimpleNamespace(id=10_000 + i, owner_id=other.id, team_id="T", channel_id="C", text=m.text, priority_label="P1", author_name="x",
                              user_id="U", message_created_at=m.message_created_at, is_bot=False) for i, m in enumerate(mine[:5])]
    integrations.index_messages(theirs)
    hits = integrations.search_vectors(mine[0].text, user.id, limit=50)
    assert hits and all(h["message_id"] < 10_000 for h in hits)           # never another user's points
    integrations.set_message_projects(user.id, {mine[0].id: [7]})
    only = integrations.search_vectors(mine[0].text, user.id, limit=50, project_ids=[7])
    assert [h["message_id"] for h in only] == [mine[0].id]
    integrations.index_summary(user.id, 7, "Phoenix", "Phoenix cutover is blocked on DNS")
    assert integrations.search_vectors("phoenix dns", user.id, kind="summary")[0]["project_id"] == 7
    assert integrations.search_vectors("phoenix dns", other.id, kind="summary") == []
    integrations.delete_summary_index([7])
    assert integrations.search_vectors("phoenix dns", user.id, kind="summary") == []


def test_qdrant_outage_never_raises(monkeypatch):
    monkeypatch.setattr(settings, "qdrant_url", "http://down")
    monkeypatch.setattr(integrations, "_qdrant", lambda: (_ for _ in ()).throw(ConnectionError("boom")))
    assert integrations.search_vectors("x", 1) == []
    assert integrations.index_summary(1, 1, "n", "t").startswith("error")


def test_every_cypher_query_is_owner_scoped(monkeypatch):  # noqa: C901
    queries = []

    class Sess:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def run(self, q, **kw):
            queries.append((q, kw)); return []

    monkeypatch.setattr(settings, "neo4j_uri", "bolt://x")
    monkeypatch.setattr(integrations, "_neo4j_driver", lambda: SimpleNamespace(session=lambda **k: Sess()))
    integrations.sync_project_graph(5, [{"id": 1, "name": "P"}], {9: [1]})
    integrations.graph_project_people(5, 1)
    integrations.graph_related_projects(5, 1)
    integrations.graph_people(5)
    integrations.graph_person_projects(5, "Priya")
    integrations.graph_works_with(5, "Priya")
    integrations.graph_project_authors(5)
    assert len(queries) >= 9
    for q, kw in queries:
        assert "owner" in q.lower() and (kw.get("owner") == 5 or "r.owner" in q), q


def test_semantic_link_and_collaborator_boost(db, user, qdrant, monkeypatch):
    from datetime import timedelta
    from app import projects
    from app.models import MessageProject, Project, SlackConnection, utcnow
    monkeypatch.setattr(settings, "semantic_link_min_score", 0.15)
    demo.seed_demo(db, user, size="full", process=True)
    ph = db.query(Project).filter(Project.name.ilike("phoenix%")).first()
    assert integrations.qdrant_stats(user.id)["summaries"] >= 1
    conn = db.query(SlackConnection).filter_by(owner_id=user.id, provider="slack").first()
    # an important message that never says "Phoenix" but is about its summary content (cutover / replication / DNS)
    m = SlackMessage(owner_id=user.id, connection_id=conn.id, team_id=conn.team_id, channel_id="CDEMOPLAT", conv_type="public_channel", slack_ts="1999999999.000001",
                     text="Urgent: the cutover plan, replication lag and DNS change are blocked on the vendor reply, deadline tomorrow",
                     raw_text="x", priority_score=70, priority_label="P1", message_created_at=utcnow() - timedelta(hours=1), author_name="Someone")
    db.add(m); db.commit()
    assert db.query(MessageProject).filter_by(message_id=m.id).count() == 0
    out = projects.semantic_link(db, user.id)
    assert out["linked"] >= 1
    link = db.query(MessageProject).filter_by(message_id=m.id).first()
    assert link is not None and link.hits == 0 and link.owner_id == user.id
    # key collaborators (PostgreSQL path, Neo4j off) get a bounded boost, once
    people, src = projects.key_collaborators(db, user.id)
    assert src == "postgresql" and people
    r1 = projects.apply_people_boost(db, user.id)
    r2 = projects.apply_people_boost(db, user.id)
    assert r2["boosted"] == 0
    assert db.query(SlackMessage).filter(SlackMessage.reason.like("%key collaborator%")).count() >= r1["boosted"] > -1
    assert db.query(SlackMessage).filter(SlackMessage.reason.like("%key collaborator%")).count() > 0  # the pipeline already applied it
    assert db.query(SlackMessage).filter(SlackMessage.reason.like("%key collaborator%"), SlackMessage.priority_score < 25).count() == 0
    assert db.query(SlackMessage).filter(SlackMessage.reason.like("%key collaborator%"), SlackMessage.priority_score > 64).count() == 0
    assert ph is not None


def test_graph_key_people_used_when_neo4j_on(db, user, monkeypatch):
    from app import projects
    monkeypatch.setattr(integrations, "graph_key_people", lambda o, mp, mm, limit=40: [{"name": "Priya Nair", "projects": 3, "messages": 9}])
    monkeypatch.setattr(integrations, "graph_related_projects", lambda o, p, l=5: [{"id": 2, "name": "Atlas", "shared_people": 2}])
    assert projects.key_collaborators(db, user.id) == ([{"name": "Priya Nair", "projects": 3, "messages": 9}], "neo4j")
    assert projects.related_projects(db, user.id, 1) == ([{"id": 2, "name": "Atlas", "shared_people": 2}], "neo4j")


def test_selfcheck_endpoint_reports_everything(db, user, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr(settings, "admin_emails", "sc@example.com")   # the Pipeline endpoints are admin-only
    with TestClient(app, base_url="http://localhost:8000") as c:
        r = c.post("/auth/register", json={"email": "sc@example.com", "password": "correct horse battery"}, headers={"Origin": "http://localhost:8000"})
        assert r.status_code == 200
        out = c.get("/api/selfcheck").json()
        ids = {x["id"] for x in out["checks"]}
        assert {"worker", "slack", "gmail", "projects", "qdrant", "neo4j", "collab", "events"} <= ids
        assert all(x["status"] in ("ok", "warn", "off", "fail") for x in out["checks"])
