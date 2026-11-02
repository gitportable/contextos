import json
from datetime import datetime, timedelta, timezone

from app.models import ProcessingJob, Reminder, SlackMessage, SuggestedEvent
from app.pipeline import apply_deletion, ingest_message


def ts(minutes_ago=5):
    return f"{(datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).timestamp():.6f}"


def test_create_duplicate_edit_delete(db, conn):
    t = ts()
    raw = {"ts": t, "user": "UOTHER", "text": "Please send the invoice by tomorrow"}
    assert ingest_message(db, conn, "C1", raw).action == "created"
    assert ingest_message(db, conn, "C1", raw).action == "duplicate"  # Slack redelivery
    db.commit()
    row = db.query(SlackMessage).one()
    assert db.query(Reminder).filter_by(message_id=row.id).count() == 0

    edited = {**raw, "text": "Please send the invoice by Friday", "edited": {"ts": ts(1)}}
    assert ingest_message(db, conn, "C1", edited).action == "updated"
    stale = {**raw, "text": "old text", "edited": {"ts": ts(30)}}
    assert ingest_message(db, conn, "C1", stale).action == "duplicate"  # out-of-order older edit must not win
    db.commit()
    assert db.query(Reminder).filter_by(message_id=row.id).count() == 0  # message text alone does not create reminders

    assert apply_deletion(db, conn, "C1", t).action == "deleted"
    db.commit()
    assert db.query(Reminder).filter_by(message_id=row.id).count() == 0


def test_own_messages_do_not_create_reminders(db, conn):
    ingest_message(db, conn, "C1", {"ts": ts(), "user": "UME", "text": "please send this by tomorrow"})
    db.commit()
    assert db.query(Reminder).count() == 0 and db.query(SlackMessage).one().is_self


def test_live_message_cache_is_invalidated_before_external_indexing(db, conn, monkeypatch):
    from app import slack_events

    calls = []
    monkeypatch.setattr(slack_events, "invalidate_user_caches", lambda owner_id: calls.append(("invalidate", owner_id)))
    monkeypatch.setattr(slack_events, "link_and_log", lambda *args, **kwargs: None)

    def index_rows(*args, **kwargs):
        assert calls == [("invalidate", conn.owner_id)]
        return {}

    monkeypatch.setattr(slack_events, "index_rows", index_rows)
    message_ts = ts()
    result = slack_events.process_message_job(db, {
        "connection_id": conn.id,
        "channel_id": "C1",
        "ts": message_ts,
        "op": "upsert",
        "message": {"ts": message_ts, "text": "A new update from the team"},
    })

    assert result["action"] == "created"


def test_unsupported_subtypes_ignored(db, conn):
    assert ingest_message(db, conn, "C1", {"ts": ts(), "subtype": "channel_join", "text": "x joined"}).action == "ignored"


def test_stale_history_does_not_create_overdue_reminder(db, conn):
    old = f"{(datetime.now(timezone.utc) - timedelta(days=10)).timestamp():.6f}"
    ingest_message(db, conn, "C1", {"ts": old, "user": "UO", "text": "send by tomorrow please"}, source="history")
    db.commit()
    assert db.query(Reminder).count() == 0


def test_message_text_does_not_create_unconfirmed_artifacts(db, conn):
    ingest_message(db, conn, "D1", {"ts": ts(), "user": "UO", "text": "let's schedule a call tomorrow at 3pm"}, conv_type="im")
    db.commit()
    assert db.query(SuggestedEvent).count() == 0
    assert db.query(Reminder).count() == 0


def test_fanout_is_per_connection_and_private(db, conn, user):
    """Two users in one workspace: only the member of the channel receives the message."""
    from app.models import SlackConnection, SlackConversation, User
    from app.slack_events import process_slack_event
    other = User(email="b@example.com", name="B", password_hash="x"); db.add(other); db.commit()
    c2 = SlackConnection(owner_id=other.id, team_id="T1", slack_user_id="UOTHERME", user_token_encrypted="x", granted_user_scopes="")
    db.add_all([c2, SlackConversation(connection_id=conn.id, channel_id="G1", conv_type="private_channel")]); db.commit()
    payload = {"team_id": "T1", "event_id": "Ev1", "event": {"type": "message", "channel": "G1", "channel_type": "group", "user": "UX", "text": "secret", "ts": ts()}}
    out = process_slack_event(db, payload)
    assert out["fanout"] == 1
    job = db.query(ProcessingJob).one()
    assert json.loads(job.payload_json)["connection_id"] == conn.id
