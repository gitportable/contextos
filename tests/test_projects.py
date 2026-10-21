import time

from app import projects
from app.models import Project, SlackMessage
from app.pipeline import ingest_message


def _ts(i):
    return f"{time.time() - 600 + i * 0.01:.6f}"


def test_discovery_tags_and_inherits_thread(db, conn):
    texts = ["Kicking off Project Atlas, need the cutover plan", "Atlas migration is blocked on the ledger schema", "Can you review the Atlas Migration runbook?",
             "Orion launch date confirmed", "lunch?"]
    for i, t in enumerate(texts):
        ingest_message(db, conn, "C1", {"ts": _ts(i), "user": f"U{i % 3}", "text": t}, source="history")
    reply = {"ts": _ts(9), "user": "U7", "text": "looks good, merging now", "thread_ts": _ts(0)}
    ingest_message(db, conn, "C1", reply, source="history")
    db.commit()
    res = projects.discover(db, conn.owner_id)
    names = {p.name for p in db.query(Project).all()}
    assert any("Atlas" in n for n in names) and not any("lunch" in n.lower() for n in names)
    atlas = next(p for p in db.query(Project).all() if "atlas" in p.keywords)
    assert atlas.message_count >= 3 and res["tagged"] >= 3


def test_user_rename_survives_rediscovery(db, conn):
    for i in range(4):
        ingest_message(db, conn, "C1", {"ts": _ts(i), "user": f"U{i % 2}", "text": f"Project Helios status {i}: Helios revamp on track"}, source="history")
    db.commit(); projects.discover(db, conn.owner_id)
    p = db.query(Project).filter_by(slug="helios").one(); p.name, p.renamed = "Helios (Analytics)", True; db.commit()
    projects.discover(db, conn.owner_id)
    assert db.query(Project).filter_by(slug="helios").one().name == "Helios (Analytics)"


def test_discovery_keeps_multiword_project_channel_names(db, conn):
    for i, author in enumerate(("U1", "U2", "U3")):
        ingest_message(db, conn, "C1", {"ts": _ts(i), "user": author,
                                       "text": "Planning the launch in #proj-nebula-launch"}, source="history")
    db.commit()
    projects.discover(db, conn.owner_id)

    project = db.query(Project).filter_by(slug="nebula").one()
    assert project.name == "Nebula Launch"
    assert "nebula launch" in project.keywords
    assert project.message_count == 3


def test_repeated_numeric_brand_title_is_discovered_as_project():
    texts = ["78catalyst - Cohort Session5 Wednesday invite", "78catalyst - Cohort Session5 cohort reminder",
             "78catalyst - Cohort Session5 joining info"]
    rows = [{"id": i, "text": text, "author": "BOT", "day": 1} for i, text in enumerate(texts, 1)]
    found = projects.analyze(rows, set())
    assert found["78catalyst"]["name"] == "78catalyst"


def test_project_matching_prefers_specific_keywords_and_respects_boundaries():
    matchers = [(1, projects.keyword_regex(["launch"])),
                (2, projects.keyword_regex(["nebula launch"]))]
    assert projects.best_project("Planning the Nebula launch", matchers) == 2
    assert projects.best_project("The launchpad is ready", matchers) is None


def test_manual_project_search_and_tagging_are_owner_scoped(db, conn):
    first = ingest_message(db, conn, "C1", {"ts": _ts(1), "user": "U1", "text": "Planning #proj-nebula-launch"}, source="history").message
    ingest_message(db, conn, "C1", {"ts": _ts(2), "user": "U2", "text": "The launchpad is ready"}, source="history")
    db.commit()

    matches = projects.matching_messages(db, conn.owner_id, ["nebula launch"])
    assert [row.id for row in matches] == [first.id]

    project, changed, count = projects.create_manual(db, conn.owner_id, "Nebula Launch", ["nebula launch"])
    db.refresh(first)
    assert changed == count == 1
    assert first.project_id == project.id
    assert project.message_count == 1
