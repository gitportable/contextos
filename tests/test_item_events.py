import json
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from app.item_events import record_message_event
from app.models import Item, ItemEvent, User
from app.pipeline import ingest_message, purge_connection_data


def now_ts():
    return f"{datetime.now(timezone.utc).timestamp():.6f}"


def test_items_preserve_direction_and_exact_source_ids(db, conn):
    message = ingest_message(db, conn, "C1", {
        "ts": now_ts(),
        "user": "UOTHER",
        "text": "I'll send the deck by Friday",
    }).message
    item = Item(
        owner_id=conn.owner_id,
        connection_id=conn.id,
        kind="commitment",
        owner_role="me",
        direction="i_promised",
        state="OPEN",
        source_message_ids=json.dumps(sorted([message.id, 12, 3])),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    assert item.owner_role == "me"
    assert item.direction == "i_promised"
    assert json.loads(item.source_message_ids) == sorted([message.id, 12, 3])


def test_user_actions_are_saved_as_content_free_ranking_events(db, conn):
    message = ingest_message(db, conn, "C1", {
        "ts": now_ts(),
        "user": "UOTHER",
        "text": "Please review",
    }).message
    record_message_event(db, owner_id=conn.owner_id, message_id=message.id, event_type="snooze", payload={"hours": 4})
    db.commit()

    event = db.query(ItemEvent).one()
    assert event.owner_id == conn.owner_id
    assert event.message_id == message.id
    assert event.event_type == "snooze"
    assert json.loads(event.payload_json) == {"hours": 4}
    with pytest.raises(ValueError):
        record_message_event(db, owner_id=conn.owner_id, message_id=message.id, event_type="snooze",
                             payload={"text": message.text})


def test_ranking_feedback_endpoint_is_owner_scoped_and_logged(db, conn, user):
    from app.routes_intelligence import RankingFeedbackIn, ranking_feedback

    message = ingest_message(db, conn, "C1", {
        "ts": now_ts(),
        "user": "UOTHER",
        "text": "FYI",
    }).message
    result = ranking_feedback(message.id, RankingFeedbackIn(label="not_urgent"), user=user, db=db)
    assert result == {"ok": True, "message_id": message.id, "label": "not_urgent"}
    event = db.query(ItemEvent).one()
    assert event.message_id == message.id
    assert json.loads(event.payload_json) == {"label": "not_urgent"}

    other = User(email="other@example.com", name="Other", password_hash="x")
    db.add(other)
    db.commit()
    with pytest.raises(HTTPException) as error:
        ranking_feedback(message.id, RankingFeedbackIn(label="always_show"), user=other, db=db)
    assert error.value.status_code == 404
    assert db.query(ItemEvent).count() == 1


def test_connection_purge_deletes_its_items_and_feedback(db, conn):
    item = Item(owner_id=conn.owner_id, connection_id=conn.id, direction="they_promised", owner_role="other")
    db.add(item)
    db.flush()
    db.add(ItemEvent(owner_id=conn.owner_id, item_id=item.id, event_type="act"))
    db.commit()

    purge_connection_data(db, conn)
    db.commit()
    assert db.query(Item).filter_by(connection_id=conn.id).count() == 0
    assert db.query(ItemEvent).filter_by(owner_id=conn.owner_id).count() == 0
