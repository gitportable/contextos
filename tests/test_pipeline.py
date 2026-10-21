import json
from datetime import datetime, timedelta, timezone

from app.models import ProcessingJob, Reminder, SlackMessage, SuggestedEvent, as_utc
from app.pipeline import apply_deletion, ingest_message
from app.priority import get_tz


def ts(minutes_ago=5):
    return f"{(datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).timestamp():.6f}"


def test_create_duplicate_edit_delete(db, conn):
    t = ts()
    raw = {"ts": t, "user": "UOTHER", "text": "Please send the invoice by tomorrow"}
    assert ingest_message(db, conn, "C1", raw).action == "created"
    assert ingest_message(db, conn, "C1", raw).action == "duplicate"  # Slack redelivery
    db.commit()
    row = db.query(SlackMessage).one()
    assert db.query(Reminder).filter_by(message_id=row.id).count() == 1

    edited = {**raw, "text": "Please send the invoice by Friday", "edited": {"ts": ts(1)}}
    assert ingest_message(db, conn, "C1", edited).action == "updated"
    stale = {**raw, "text": "old text", "edited": {"ts": ts(30)}}
    assert ingest_message(db, conn, "C1", stale).action == "duplicate"  # out-of-order older edit must not win
    db.commit()
    assert db.query(Reminder).filter_by(message_id=row.id).count() == 1  # updated, not duplicated

    assert apply_deletion(db, conn, "C1", t).action == "deleted"
    db.commit()
    assert db.query(Reminder).filter_by(message_id=row.id).one().status == "cancelled"


def test_own_messages_do_not_create_reminders(db, conn):
    ingest_message(db, conn, "C1", {"ts": ts(), "user": "UME", "text": "please send this by tomorrow"})
    db.commit()
    assert db.query(Reminder).count() == 0 and db.query(SlackMessage).one().is_self


def test_unsupported_subtypes_ignored(db, conn):
    assert ingest_message(db, conn, "C1", {"ts": ts(), "subtype": "channel_join", "text": "x joined"}).action == "ignored"


def test_stale_history_does_not_create_overdue_reminder(db, conn):
    old = f"{(datetime.now(timezone.utc) - timedelta(days=10)).timestamp():.6f}"
    ingest_message(db, conn, "C1", {"ts": old, "user": "UO", "text": "send by tomorrow please"}, source="history")
    db.commit()
    assert db.query(Reminder).count() == 0


def test_meeting_suggestion_created(db, conn):
    ingest_message(db, conn, "D1", {"ts": ts(), "user": "UO", "text": "let's schedule a call tomorrow at 3pm"}, conv_type="im")
    db.commit()
    assert db.query(SuggestedEvent).one().is_provisional is False


def test_meeting_message_is_priority_and_creates_schedule_suggestion(db, conn):
    raw = {"ts": ts(), "user": "UOTHER", "text": "meet at 8"}
    row = ingest_message(db, conn, "C1", raw).message
    db.commit()
    assert row.priority_label == "P2"
    suggestion = db.query(SuggestedEvent).filter_by(message_id=row.id).one()
    assert suggestion.is_provisional is True
    row.priority_score, row.priority_label = 10, "P3"
    db.commit()
    assert ingest_message(db, conn, "C1", raw).action == "duplicate"
    assert row.priority_label == "P2"


def test_today_meeting_remains_visible_after_inferred_time_passes(db, conn):
    raw = {"ts": ts(minutes_ago=240), "user": "UOTHER", "text": "meet today"}
    row = ingest_message(db, conn, "C1", raw).message
    db.commit()
    suggestion = db.query(SuggestedEvent).filter_by(message_id=row.id).one()
    suggestion.status = "cancelled"
    db.commit()

    assert ingest_message(db, conn, "C1", raw).action == "duplicate"
    assert suggestion.status == "suggested"


def test_calendar_invites_use_their_stated_date_and_dedupe(db, conn):
    text = "78catalyst - Cohort Session5 Wednesday, October 21 · 8:00 – 9:00pm Google Meet joining info"
    first = ingest_message(db, conn, "C1", {"ts": ts(), "user": "U1", "text": text}).message
    second = ingest_message(db, conn, "C1", {"ts": ts(1), "user": "U2", "text": text}).message
    db.commit()

    events = db.query(SuggestedEvent).filter_by(owner_id=conn.owner_id, status="suggested").all()
    assert len(events) == 1
    assert events[0].message_id == first.id
    assert as_utc(events[0].proposed_start).astimezone(get_tz()).strftime("%Y-%m-%d %H:%M") == "2026-10-21 20:00"
    assert events[0].is_provisional is False


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
