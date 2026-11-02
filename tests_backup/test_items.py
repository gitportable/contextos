import pytest
from fastapi import HTTPException

from app.crypto import encrypt_token
from app.item_ranker import rank_item, urgency_score
from app.models import Item, ItemEvent, ItemSource, SlackMessage, User
from app.routes_items import ItemCreate, ItemPatch, brief, create_item, delete_item, list_items, update_item


def test_urgency_is_bounded_and_increases_as_deadline_approaches():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    values = [
        urgency_score(now=now, due_at=now + timedelta(days=2), effort_minutes=30),
        urgency_score(now=now, due_at=now + timedelta(hours=2), effort_minutes=30),
        urgency_score(now=now, due_at=now - timedelta(days=2), effort_minutes=30),
    ]
    assert all(0 <= score <= 1 for score in values)
    assert values == sorted(values)


def test_item_create_encrypts_title_links_only_owned_sources(db, conn, user):
    source = SlackMessage(owner_id=user.id, connection_id=conn.id, team_id=conn.team_id, channel_id="C1",
                          slack_ts="1.0", text="Please review this")
    db.add(source)
    db.commit()

    result = create_item(ItemCreate(title="Review launch", direction="asked_of_me",
                                    source_message_ids=[source.id]), user, db)
    stored = db.query(Item).filter_by(id=result["id"]).one()
    assert stored.title_encrypted != "Review launch"
    assert result["title"] == "Review launch"
    assert result["source_message_ids"] == [source.id]
    assert result["sources"][0]["text"] == "Please review this"
    assert db.query(ItemEvent).filter_by(item_id=stored.id, event_type="act").count() == 1

    other = User(email="other@example.com", name="Other", password_hash="x")
    db.add(other)
    db.commit()
    with pytest.raises(HTTPException) as exc:
        create_item(ItemCreate(title="Not mine", direction="asked_of_me",
                               source_message_ids=[source.id]), other, db)
    assert exc.value.status_code == 404


def test_item_state_transition_uses_optimistic_version(db, user):
    item = Item(owner_id=user.id, kind="commitment", owner_role="me", direction="asked_of_me", state="OPEN",
                title_encrypted=encrypt_token("Prepare notes"))
    db.add(item)
    db.commit()

    result = update_item(item.id, ItemPatch(expected_version=1, state="RESOLVED"), user, db)
    assert result["state"] == "RESOLVED" and result["version"] == 2
    with pytest.raises(HTTPException) as exc:
        update_item(item.id, ItemPatch(expected_version=1, state="OPEN"), user, db)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "stale_version"


def test_delete_item_keeps_content_free_dismissal_event(db, user):
    created = create_item(ItemCreate(title="Archive this", direction="asked_of_me"), user, db)
    assert delete_item(created["id"], user, db) == {"ok": True, "id": created["id"]}
    event = db.query(ItemEvent).filter_by(owner_id=user.id, event_type="dismiss").one()
    assert event.item_id is None
    assert event.payload_json == '{"reason":"deleted"}'


def test_deleted_source_is_detached_without_losing_confirmed_item(db, conn, user):
    from app.pipeline import apply_deletion
    source = SlackMessage(owner_id=user.id, connection_id=conn.id, team_id=conn.team_id, channel_id="C1",
                          slack_ts="2.0", text="Review the release")
    db.add(source)
    db.commit()
    result = create_item(ItemCreate(title="Review release", direction="asked_of_me",
                                    source_message_ids=[source.id]), user, db)

    assert apply_deletion(db, conn, "C1", "2.0").action == "deleted"
    db.commit()

    item = db.query(Item).filter_by(id=result["id"]).one()
    assert item.source_message_ids == "[]"
    assert db.query(ItemSource).filter_by(item_id=item.id).count() == 0
    assert list_items("active", 100, user, db)[0]["source_message_ids"] == []


def test_brief_is_capped_at_seven_and_ledger_is_owner_scoped(db, user):
    other = User(email="other@example.com", name="Other", password_hash="x")
    db.add(other)
    db.flush()
    db.add_all([
        Item(owner_id=user.id, kind="commitment", owner_role="me", direction="asked_of_me", state="OPEN",
             title_encrypted=encrypt_token(f"Task {index}"))
        for index in range(9)
    ])
    db.add(Item(owner_id=other.id, kind="commitment", owner_role="me", direction="asked_of_me", state="OPEN",
                title_encrypted=encrypt_token("Private task")))
    db.commit()

    result = brief(50, user, db)
    assert len(result["items"]) == 7
    assert result["total_open"] == 9
    assert result["scoring"] == "rules_only"
    assert all(item["title"] != "Private task" for item in list_items("active", 200, user, db))


def test_rules_ranker_is_bounded_and_explainable():
    from datetime import datetime, timezone
    result = rank_item(kind="commitment", direction="asked_of_me", state="OPEN", due_at=None)
    assert 0 <= result["score"] <= 1
    assert 0 <= result["urgency"] <= 1
    assert "confirmed item" in result["explanation"]


def test_item_routes_are_registered_and_round_trip(db, user):
    from fastapi.testclient import TestClient
    from app.auth import current_user
    from app.database import get_db
    from app.main import app

    def override_db():
        yield db

    app.dependency_overrides[current_user] = lambda: user
    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            created = client.post("/api/items", json={"title": "Send the proposal", "direction": "i_promised"})
            assert created.status_code == 200, created.text
            item = created.json()
            assert item["title"] == "Send the proposal"
            assert client.get("/api/items/brief").json()["items"][0]["id"] == item["id"]
            updated = client.patch(f"/api/items/{item['id']}", json={
                "expected_version": item["version"], "state": "RESOLVED",
            })
            assert updated.status_code == 200 and updated.json()["state"] == "RESOLVED"
    finally:
        app.dependency_overrides.pop(current_user, None)
        app.dependency_overrides.pop(get_db, None)
