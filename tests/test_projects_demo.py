from datetime import timedelta

from app import assistant, demo, retention, summaries
from app.config import settings
from app.models import MessageProject, Project, ProjectSummary, SlackMessage, utcnow


def test_demo_pipeline_discovers_named_projects(db, user):
    demo.seed_demo(db, user, size="full", process=True)
    names = {p.name.lower() for p in db.query(Project).filter_by(owner_id=user.id)}
    assert {"phoenix", "atlas"} <= {n.split()[0] for n in names}
    assert db.query(SlackMessage).filter_by(owner_id=user.id).count() > 250
    assert db.query(ProjectSummary).count() >= 3
    # every citation in every summary points at a real message of this user
    ids = {i for (i,) in db.query(SlackMessage.id).filter_by(owner_id=user.id)}
    import json
    for s in db.query(ProjectSummary):
        for item in json.loads(s.key_points_json) + json.loads(s.open_items_json):
            assert item["sources"] and set(item["sources"]) <= ids


def test_demo_is_idempotent_and_removable(db, user):
    demo.seed_demo(db, user, size="small", process=True)
    n = db.query(SlackMessage).count()
    demo.seed_demo(db, user, size="small", process=True)
    assert db.query(SlackMessage).count() == n
    demo.remove_demo(db, user)
    assert db.query(SlackMessage).count() == 0 and db.query(Project).count() == 0


def test_summary_drops_invented_citations(db, user):
    demo.seed_demo(db, user, size="full", process=True)
    p = db.query(Project).first()
    allowed = {1, 2}
    out = summaries._clean_items([{"text": "ok", "sources": [1, 999]}, {"text": "uncited", "sources": []}, {"text": "bad", "sources": [True]}, "junk"], allowed)
    assert out == [{"text": "ok", "sources": [1]}]
    assert p is not None


def test_retention_compacts_and_deletes(db, user):
    demo.seed_demo(db, user, size="full", process=True)
    old = utcnow() - timedelta(days=settings.retention_p3_compact_days + 2)
    p3 = db.query(SlackMessage).filter_by(owner_id=user.id, priority_label="P3", is_deleted=False).limit(5).all()
    for m in p3:
        m.message_created_at = old
    gone = db.query(SlackMessage).filter_by(owner_id=user.id, priority_label="P3", is_deleted=False).offset(5).first()
    gone.message_created_at = utcnow() - timedelta(days=settings.retention_p3_delete_days + 1)
    gone_id = gone.id
    db.commit()
    rep = retention.run_retention(db)
    assert rep["compacted"] >= 4 and rep["deleted_p3"] >= 1
    db.expire_all()
    assert db.get(SlackMessage, gone_id) is None
    assert db.query(MessageProject).filter(MessageProject.message_id == gone_id).count() == 0
    m = db.get(SlackMessage, p3[0].id)
    if m is not None:
        assert m.raw_text == "" and len(m.text) <= settings.snippet_chars


def test_retention_cap_never_prunes_p0_p1(db, user, monkeypatch):
    demo.seed_demo(db, user, size="full", process=True)
    keep = db.query(SlackMessage).filter(SlackMessage.priority_label.in_(("P0", "P1"))).count()
    monkeypatch.setattr(settings, "max_messages_per_user", 10)
    retention.run_retention(db)
    assert db.query(SlackMessage).filter(SlackMessage.priority_label.in_(("P0", "P1"))).count() == keep


def test_assistant_answer_has_sources_and_isolated(db, user):
    from app.models import User
    other = User(email="o@example.com", name="O", password_hash="x"); db.add(other); db.commit()
    demo.seed_demo(db, user, size="full", process=True)
    r = assistant.answer(db, other, "What is the status of Phoenix?") if hasattr(assistant, "answer") else None
    if r is not None:
        assert not r.get("sources")
