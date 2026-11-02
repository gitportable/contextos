"""End-to-end API checks with FastAPI's TestClient on a file-backed SQLite DB."""
import pytest

pytest.importorskip("fastapi")


@pytest.fixture()
def client(tmp_path):
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    import app.database as database
    import app.models  # noqa: F401
    engine = create_engine(f"sqlite:///{tmp_path}/t.db", connect_args={"check_same_thread": False})
    database.Base.metadata.create_all(engine)
    database.SessionLocal.configure(bind=engine)
    from app.main import app
    with TestClient(app, base_url="http://localhost:8000") as c:
        yield c


def register(c, email, pw="Str0ngPassw0rd"):
    r = c.post("/auth/register", json={"email": email, "password": pw})
    return r, r.json().get("csrf_token", "")


def test_register_validates_email_and_password(client):
    assert client.post("/auth/register", json={"email": "not-an-email", "password": "Str0ngPassw0rd"}).status_code == 400
    assert client.post("/auth/register", json={"email": "a@b.co", "password": "short"}).status_code in (400, 422)
    r, _ = register(client, "a@example.com")
    assert r.status_code == 200 and "contextos_session" in r.cookies


def test_login_lockout_after_failed_attempts(client):
    register(client, "lock@example.com")
    client.cookies.clear()
    codes = [client.post("/auth/login", json={"email": "lock@example.com", "password": "wrong-password1"}).status_code for _ in range(6)]
    assert codes[:4] == [401] * 4 and codes[4] == 429 and codes[5] == 429
    assert client.post("/auth/login", json={"email": "lock@example.com", "password": "Str0ngPassw0rd"}).status_code == 429  # even the right password is locked out


def test_csrf_and_origin_guards(client):
    r, token = register(client, "csrf@example.com")
    assert client.post("/auth/logout").status_code == 403                                  # no CSRF token
    assert client.post("/auth/logout", headers={"X-CSRF-Token": token, "Origin": "https://evil.example"}).status_code == 403
    assert client.post("/auth/logout", headers={"X-CSRF-Token": token}).status_code == 200


def test_users_cannot_read_each_others_data(client):
    from app.models import SlackConnection, SlackMessage, User
    import app.database as database
    _, _ = register(client, "owner@example.com")
    with database.SessionLocal() as db:
        owner = db.query(User).filter_by(email="owner@example.com").one()
        c = SlackConnection(owner_id=owner.id, team_id="T1", slack_user_id="U1", granted_user_scopes="")
        db.add(c); db.flush()
        m = SlackMessage(owner_id=owner.id, connection_id=c.id, team_id="T1", channel_id="C1", slack_ts="1.0", text="secret")
        db.add(m); db.commit(); mid = m.id
    assert client.get(f"/messages/{mid}").status_code == 200
    client.cookies.clear()
    register(client, "intruder@example.com")
    assert client.get(f"/messages/{mid}").status_code == 404
    assert client.get("/messages").json() == []


def test_inbox_important_shows_best_message_per_priority_and_urgent_filter(client, monkeypatch):
    from app.models import SlackConnection, SlackMessage, User
    from app.config import settings
    import app.database as database

    monkeypatch.setattr(settings, "public_base_url", "http://localhost:8000")
    registered, _ = register(client, "priority-inbox@example.com")
    assert registered.status_code == 200, registered.text
    with database.SessionLocal() as db:
        owner = db.query(User).filter_by(email="priority-inbox@example.com").one()
        connection = SlackConnection(
            owner_id=owner.id, team_id="gmail:priority-inbox@example.com",
            slack_user_id="priority-inbox@example.com", granted_user_scopes="",
            provider="gmail",
        )
        db.add(connection)
        db.flush()
        for index, priority in enumerate(("P0", "P1", "P2", "P3")):
            for score in (10, 90):
                db.add(SlackMessage(
                    owner_id=owner.id, connection_id=connection.id, team_id=connection.team_id,
                    channel_id=f"thread-{index}-{score}", slack_ts=f"{index}-{score}",
                    text=("Please approve the invoice by 5pm today" if (priority, score) == ("P1", 90) else f"{priority} score {score}"),
                    priority_label=priority, priority_score=score, mentions_me=(priority == "P1" and score == 90),
                ))
        db.commit()

    important_response = client.get("/messages", params={"priority": "important", "source": "gmail"})
    assert important_response.status_code == 200, important_response.text
    important = important_response.json()
    assert [message["priority_label"] for message in important] == ["P0", "P1", "P2", "P3"]
    assert [message["priority_score"] for message in important] == [90, 90, 90, 90]

    # Urgent is strict: every critical P0, but a P1 only when it is addressed to the user (the P1 at score 10 was not).
    urgent = client.get("/messages", params={"priority": "urgent", "source": "gmail"}).json()
    assert [(m["priority_label"], m["priority_score"]) for m in urgent] == [("P0", 90), ("P0", 10), ("P1", 90)] or \
        {(m["priority_label"], m["priority_score"]) for m in urgent} == {("P0", 90), ("P0", 10), ("P1", 90)}
    assert client.get("/dashboard/summary").json()["urgent"] == 3

    high = client.get("/messages", params={"priority": "high", "source": "gmail"}).json()
    assert {message["priority_label"] for message in high} == {"P1"}
    normal = client.get("/messages", params={"priority": "normal", "source": "gmail"}).json()
    assert {message["priority_label"] for message in normal} == {"P2", "P3"}


def test_urgent_collapses_a_thread_and_done_snooze_clear_it(client):
    """Twelve messages in one thread are one card; marking it Done (or snoozing) removes it and the urgent counter drops."""
    from datetime import datetime, timezone
    from app.models import SlackConnection, SlackMessage, User
    import app.database as database

    registered, csrf = register(client, "triage@example.com")
    h = {"X-CSRF-Token": csrf}
    now = datetime.now(timezone.utc)
    with database.SessionLocal() as db:
        owner = db.query(User).filter_by(email="triage@example.com").one()
        connection = SlackConnection(owner_id=owner.id, team_id="T9", slack_user_id="U9", granted_user_scopes="")
        db.add(connection)
        db.flush()
        ids = []
        for i in range(12):
            row = SlackMessage(owner_id=owner.id, connection_id=connection.id, team_id="T9", channel_id="C-thread", thread_ts="1.0", slack_ts=f"1.{i}",
                               text=f"prod down again #{i}", priority_label="P0", priority_score=90 - i, message_created_at=now, mentions_me=True)
            db.add(row); db.flush(); ids.append(row.id)
        bot = SlackMessage(owner_id=owner.id, connection_id=connection.id, team_id="T9", channel_id="C-bot", slack_ts="2.0", is_bot=True,
                           text="alert: CPU high", priority_label="P0", priority_score=95, message_created_at=now)
        old = SlackMessage(owner_id=owner.id, connection_id=connection.id, team_id="T9", channel_id="C-old", slack_ts="3.0", mentions_me=True,
                           text="old ask", priority_label="P1", priority_score=80, message_created_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
        db.add_all([bot, old]); db.commit()
    urgent = client.get("/messages", params={"priority": "urgent"}).json()
    assert len(urgent) == 1 and urgent[0]["more_in_conversation"] == 11 and urgent[0]["priority_score"] == 90   # bot + stale excluded
    assert client.get("/dashboard/summary").json()["urgent"] == 1
    done = client.post(f"/messages/{urgent[0]['id']}/handled", json={}, headers=h)
    assert done.status_code == 200 and done.json()["cleared"] == 12
    assert client.get("/messages", params={"priority": "urgent"}).json() == []
    assert client.get("/dashboard/summary").json()["urgent"] == 0
    assert client.post("/messages/triage/undo", json={"ids": done.json()["ids"]}, headers=h).json()["restored"] == 12
    assert len(client.get("/messages", params={"priority": "urgent"}).json()) == 1
    assert client.post(f"/messages/{ids[0]}/snooze", json={"hours": 4}, headers=h).status_code == 200
    assert client.get("/messages", params={"priority": "urgent"}).json() == []


def test_someone_elses_message_cannot_be_triaged(client):
    from app.models import SlackConnection, SlackMessage, User
    import app.database as database
    register(client, "owner1@example.com")
    with database.SessionLocal() as db:
        owner = db.query(User).filter_by(email="owner1@example.com").one()
        c = SlackConnection(owner_id=owner.id, team_id="T7", slack_user_id="U7", granted_user_scopes="")
        db.add(c); db.flush()
        m = SlackMessage(owner_id=owner.id, connection_id=c.id, team_id="T7", channel_id="C", slack_ts="1.0", text="x", priority_label="P0")
        db.add(m); db.commit(); mid = m.id
    client.cookies.clear()
    _, csrf = register(client, "intruder@example.com")
    assert client.post(f"/messages/{mid}/handled", json={}, headers={"X-CSRF-Token": csrf}).status_code == 404
    assert client.post(f"/messages/{mid}/snooze", json={}, headers={"X-CSRF-Token": csrf}).status_code == 404


def test_security_headers_and_unauthenticated_api(client):
    r = client.get("/api/config")
    assert "script-src 'self'" in r.headers["content-security-policy"] and r.headers["x-frame-options"] == "DENY"
    assert client.get("/messages").status_code == 401


def test_owner_only_admin_and_pipeline_routes(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "admin_emails", "owner@example.com")
    admin, _ = register(client, "owner@example.com")
    assert admin.status_code == 200 and admin.json()["user"]["is_admin"] is True
    assert client.get("/api/pipeline/overview").status_code == 200
    assert client.get("/api/pipeline/events").status_code == 200
    assert client.get("/api/intelligence/status").status_code == 200
    client.cookies.clear()
    member, _ = register(client, "member@example.com")
    assert member.json()["user"]["is_admin"] is False
    for path in ("/api/pipeline/overview", "/api/pipeline/events", "/api/selfcheck", "/api/intelligence/status", "/queue/summary"):
        assert client.get(path).status_code == 403, path


def test_account_profile_password_and_session_controls(client):
    from app import auth
    import app.database as database
    from app.models import User
    response, csrf = register(client, "account@example.com")
    headers = {"X-CSRF-Token": csrf}
    assert response.json()["user"]["has_password"] is True
    assert client.patch("/auth/account", json={"name": "Account Owner"}, headers=headers).json()["name"] == "Account Owner"
    assert client.post("/auth/password", json={"current_password": "wrong", "new_password": "An0therStrongPassw0rd"}, headers=headers).status_code == 400
    assert client.post("/auth/password", json={"current_password": "Str0ngPassw0rd", "new_password": "An0therStrongPassw0rd"}, headers=headers).status_code == 200
    with database.SessionLocal() as db:
        user = db.query(User).filter_by(email="account@example.com").one()
        _, other = auth.create_session(db, user)
        other_id = other.id
    sessions = client.get("/auth/sessions").json()
    current = next(row for row in sessions if row["current"])
    assert client.delete(f"/auth/sessions/{current['id']}", headers=headers).status_code == 400
    assert client.delete(f"/auth/sessions/{other_id}", headers=headers).status_code == 200


def test_commitments_assign_and_filter_by_connected_slack_people(client):
    from datetime import datetime, timezone
    import app.database as database
    from app.models import SlackConnection, SlackUser, User, utcnow
    response, csrf = register(client, "planner@example.com")
    headers = {"X-CSRF-Token": csrf}
    with database.SessionLocal() as db:
        user = db.query(User).filter_by(email="planner@example.com").one()
        conn = SlackConnection(owner_id=user.id, team_id="T1", slack_user_id="U1", status="connected",
                               user_token_encrypted="", granted_user_scopes="users:read", people_synced_at=utcnow())
        db.add(conn)
        db.add(SlackUser(team_id="T1", user_id="U2", display_name="Ada Lovelace", username="ada",
                         email="ada@example.com", is_bot=False))
        db.commit()
    person_id = "T1:U2"
    assert client.get("/integrations/slack/people").json()[0]["email"] == "ada@example.com"
    reminder = client.post("/reminders", json={"title": "Review proposal", "due_at": "2026-12-01T10:00:00Z",
                                               "assignees": [person_id]}, headers=headers)
    assert reminder.status_code == 200 and reminder.json()["assignees"][0]["id"] == person_id
    assert len(client.get(f"/reminders?status=all&person_id={person_id}").json()) == 1
    assert client.get("/reminders?status=all&person_id=T1%3AU1").json() == []
    event = client.post("/schedule/suggestions", json={"title": "Planning", "proposed_start": "2026-12-02T10:00:00Z",
                                                       "assignees": [person_id]}, headers=headers)
    assert event.status_code == 200 and event.json()["assignees"][0]["name"] == "Ada Lovelace"
    assert len(client.get(f"/schedule/suggestions?status=all&person_id={person_id}").json()) == 1
    invalid = client.post("/reminders", json={"title": "Invalid assignment", "due_at": datetime.now(timezone.utc).isoformat(),
                                              "assignees": ["T2:U9"]}, headers=headers)
    assert invalid.status_code == 400


def test_disconnected_slack_connection_remains_visible_and_demo_can_be_restored(client, monkeypatch):
    import app.main as main
    import app.database as database
    import app.demo as demo
    from app.models import SlackConnection, User
    from app.demo import demo_team

    monkeypatch.setattr(main.ratelimit, "hit", lambda *args, **kwargs: (True, 0))
    response, csrf = register(client, "demorestore@example.com")
    assert response.status_code == 200, response.text
    headers = {"X-CSRF-Token": csrf}
    with database.SessionLocal() as db:
        user = db.query(User).filter_by(email="demorestore@example.com").one()
        conn = SlackConnection(owner_id=user.id, team_id=demo_team(user.id), slack_user_id="UDEMOYOU",
                              team_name="Acme (demo workspace)", status="disconnected", is_demo=True)
        db.add(conn)
        db.commit()
        connection_id = conn.id

    connections = client.get("/integrations/slack/connection").json()["connections"]
    assert any(c["id"] == connection_id and c["status"] == "disconnected" for c in connections)

    monkeypatch.setattr(demo, "build_events", lambda *args, **kwargs: [])
    restored = client.post("/api/demo/seed?process=false", headers=headers)
    assert restored.status_code == 200
    assert client.get("/integrations/slack/connection").json()["connections"][0]["status"] == "connected"


def test_meeting_message_priority_is_exposed_in_today_and_schedule(client, monkeypatch):
    from datetime import datetime, timedelta, timezone
    import app.database as database
    import app.main as main
    from app.models import SlackConnection, SlackMessage, SuggestedEvent, User

    monkeypatch.setattr(main.ratelimit, "hit", lambda *args, **kwargs: (True, 0))
    response, _ = register(client, "meetingpriority@example.com")
    assert response.status_code == 200, response.text
    with database.SessionLocal() as db:
        user = db.query(User).filter_by(email="meetingpriority@example.com").one()
        conn = SlackConnection(owner_id=user.id, team_id="TMEETING", slack_user_id="UMEETING", status="connected")
        db.add(conn)
        db.flush()
        message = SlackMessage(owner_id=user.id, connection_id=conn.id, team_id=conn.team_id, channel_id="C1",
                               slack_ts="1.0", text="meet today", priority_label="P1", priority_score=65,
                               message_created_at=datetime.now(timezone.utc), is_self=True)
        db.add(message)
        db.flush()
        event = SuggestedEvent(owner_id=user.id, message_id=message.id, title="meet today",
                               proposed_start=datetime.now(timezone.utc) - timedelta(hours=3),
                               duration_minutes=30, status="suggested")
        db.add(event)
        db.add(SuggestedEvent(owner_id=user.id, message_id=message.id, title="meet today",
                              proposed_start=event.proposed_start, duration_minutes=30, status="suggested",
                              assigned_people="[]"))
        db.commit()

    today_event = next(item for item in client.get("/api/today").json()["due_today"] if item["kind"] == "event")
    schedule_event = client.get("/schedule/suggestions").json()[0]
    assert today_event["priority_label"] == schedule_event["priority_label"] == "P1"
    assert today_event["priority_score"] == schedule_event["priority_score"] == 65
    assert len(client.get("/schedule/suggestions").json()) == 1
    assert any(item["id"] == message.id for item in client.get("/messages").json())


def test_ask_today_attention_includes_linked_calendar_message(client, monkeypatch):
    from datetime import datetime, timezone
    import app.database as database
    import app.integrations as integrations
    from app.models import SlackConnection, SlackMessage, SuggestedEvent, User

    monkeypatch.setattr(integrations, "search_vectors", lambda *args, **kwargs: [])
    response, csrf = register(client, "askcalendar@example.com")
    assert response.status_code == 200
    with database.SessionLocal() as db:
        user = db.query(User).filter_by(email="askcalendar@example.com").one()
        conn = SlackConnection(owner_id=user.id, team_id="TASKCAL", slack_user_id="UASKCAL", status="connected")
        db.add(conn)
        db.flush()
        message = SlackMessage(owner_id=user.id, connection_id=conn.id, team_id=conn.team_id, channel_id="C1",
                               slack_ts="1.0", text="78catalyst Cohort Session5 Wednesday October 21 8pm Google Meet",
                               priority_label="P1", priority_score=70, message_created_at=datetime.now(timezone.utc))
        db.add(message)
        db.flush()
        db.add(SuggestedEvent(owner_id=user.id, message_id=message.id, title=message.text,
                              proposed_start=datetime.now(timezone.utc), duration_minutes=60,
                              status="suggested"))
        db.commit()

    answer = client.post("/assistant/ask", json={"question": "What needs my attention today?"},
                         headers={"X-CSRF-Token": csrf})
    assert answer.status_code == 200
    assert answer.json()["sources"][0]["text"].startswith("78catalyst")


def test_assistant_returns_conversational_fallback_with_message_sources(client, monkeypatch):
    import app.database as database
    import app.integrations as integrations
    from app.models import SlackConnection, SlackMessage, User

    monkeypatch.setattr(integrations, "search_vectors", lambda *args, **kwargs: [])
    response, csrf = register(client, "assistant-fallback@example.com")
    assert response.status_code == 200
    with database.SessionLocal() as db:
        user = db.query(User).filter_by(email="assistant-fallback@example.com").one()
        connection = SlackConnection(owner_id=user.id, team_id="ASSISTANT", slack_user_id="UASSISTANT", status="connected")
        db.add(connection)
        db.flush()
        db.add(SlackMessage(owner_id=user.id, connection_id=connection.id, team_id=connection.team_id,
                            channel_id="C1", slack_ts="1.0", text="The release review for NebulaXenon is blocked until Friday.",
                            priority_label="P1", priority_score=80))
        db.commit()

    answer = client.post("/assistant/ask", json={"question": "Tell me about NebulaXenon"},
                         headers={"X-CSRF-Token": csrf})
    assert answer.status_code == 200, answer.text
    result = answer.json()
    assert isinstance(result["answer"], str) and result["answer"]
    assert "Relevant messages:" not in result["answer"]
    assert result["sources"] and "NebulaXenon" in result["sources"][0]["text"]
    greeting = client.post("/assistant/ask", json={"question": "sup"}, headers={"X-CSRF-Token": csrf})
    assert greeting.status_code == 200
    assert greeting.json()["answer"].startswith("Hi!")
    assert greeting.json()["sources"] == []


def test_assistant_answers_meeting_today_from_suggestions(client, monkeypatch):
    from datetime import datetime, time, timedelta
    import app.assistant as assistant
    import app.database as database
    import app.integrations as integrations
    from app.models import SlackConnection, SlackMessage, SuggestedEvent, User
    from app.priority import get_tz

    monkeypatch.setattr(assistant.llm, "enabled", lambda: False)
    monkeypatch.setattr(integrations, "search_vectors", lambda *args, **kwargs: [])
    response, csrf = register(client, "meeting-answer@example.com")
    assert response.status_code == 200
    with database.SessionLocal() as db:
        user = db.query(User).filter_by(email="meeting-answer@example.com").one()
        connection = SlackConnection(owner_id=user.id, team_id="MEETINGS", slack_user_id="UMEETINGS", status="connected")
        db.add(connection)
        db.flush()
        message = SlackMessage(owner_id=user.id, connection_id=connection.id, team_id=connection.team_id,
                               channel_id="C1", slack_ts="1.0", text="Planning call for ProjectOrion today",
                               priority_label="P1", priority_score=80)
        db.add(message)
        db.flush()
        today = datetime.now(get_tz()).date()
        start = datetime.combine(today, time(16, 30), tzinfo=get_tz())
        db.add(SuggestedEvent(owner_id=user.id, message_id=message.id, title="ProjectOrion planning call",
                              proposed_start=start, duration_minutes=30, status="suggested"))
        db.commit()

    answer = client.post("/assistant/ask", json={"question": "is there any meet today?"},
                         headers={"X-CSRF-Token": csrf})
    assert answer.status_code == 200, answer.text
    result = answer.json()
    assert "meeting-related item" in result["answer"] and "\n- " not in result["answer"]
    assert "ProjectOrion planning call" in result["answer"]
    assert "[1]" in result["answer"] and result["cited"] == [1]        # numbered citation, Perplexity style
    assert result["sources"][0]["message_id"] == message.id and result["sources"][0]["n"] == 1


def test_user_can_search_messages_and_create_project_from_keywords(client, monkeypatch):
    import app.database as database
    import app.routes_app as routes_app
    from app.models import SlackConnection, SlackMessage, User

    monkeypatch.setattr(routes_app, "limit_or_429", lambda *args, **kwargs: None)
    response, csrf = register(client, "projectcreate@example.com")
    assert response.status_code == 200
    with database.SessionLocal() as db:
        user = db.query(User).filter_by(email="projectcreate@example.com").one()
        conn = SlackConnection(owner_id=user.id, team_id="TPROJECT", slack_user_id="UPROJECT", status="connected")
        db.add(conn)
        db.flush()
        db.add_all([
            SlackMessage(owner_id=user.id, connection_id=conn.id, team_id=conn.team_id, channel_id="C1",
                         slack_ts="1.0", text="Nebula-launch planning", priority_label="P2"),
            SlackMessage(owner_id=user.id, connection_id=conn.id, team_id=conn.team_id, channel_id="C1",
                         slack_ts="2.0", text="Nebula launches next month", priority_label="P3"),
        ])
        db.commit()

    preview = client.get("/api/projects/matches", params={"keywords": "nebula launch"})
    assert preview.status_code == 200 and preview.json()["count"] == 1
    created = client.post("/api/projects", json={"name": "Nebula Launch", "keywords": "nebula launch"},
                          headers={"X-CSRF-Token": csrf})
    assert created.status_code == 200, created.text
    assert created.json()["matched"] == created.json()["tagged"] == 1
    project_id = created.json()["id"]
    messages = client.get("/messages", params={"project_id": project_id})
    assert messages.status_code == 200
    assert [message["text"] for message in messages.json()] == ["Nebula-launch planning"]


def test_feedback_labels_clear_pin_and_undo(client):
    """'Not urgent' clears the conversation now; 'Always show' pins; Undo removes the label; nobody else can label your messages."""
    from app.models import MessageFeedback, SlackConnection, SlackMessage, User
    import app.database as database
    _, csrf = register(client, "labels@example.com")
    h = {"X-CSRF-Token": csrf}
    with database.SessionLocal() as db:
        owner = db.query(User).filter_by(email="labels@example.com").one()
        c = SlackConnection(owner_id=owner.id, team_id="T-labels", slack_user_id="U1", granted_user_scopes="", provider="slack")
        db.add(c); db.flush()
        ids = []
        for i in range(2):
            m = SlackMessage(owner_id=owner.id, connection_id=c.id, team_id="T-labels", channel_id=f"D{i}", conv_type="im", slack_ts=f"1.{i}",
                             user_id="UBOSS", author_name="Boss", text="Please approve the invoice by 5pm today", priority_label="P1", priority_score=80)
            db.add(m); db.flush(); ids.append(m.id)
        db.commit()
    assert client.get("/dashboard/summary").json()["urgent"] == 2
    r = client.post(f"/messages/{ids[0]}/feedback", json={"label": "not_urgent"}, headers=h)
    assert r.status_code == 200 and r.json()["cleared"] >= 1
    assert client.get("/dashboard/summary").json()["urgent"] == 1
    undo = client.post("/messages/triage/undo", json={"ids": r.json()["ids"]}, headers=h)
    assert undo.status_code == 200 and client.get("/dashboard/summary").json()["urgent"] == 2
    with database.SessionLocal() as db:
        assert db.query(MessageFeedback).count() == 0
    assert client.post(f"/messages/{ids[1]}/feedback", json={"label": "always_show"}, headers=h).status_code == 200
    assert client.post(f"/messages/{ids[1]}/feedback", json={"label": "bogus"}, headers=h).status_code == 422
    _, csrf2 = register(client, "other-labels@example.com")
    h = {"X-CSRF-Token": csrf2}
    assert client.post(f"/messages/{ids[0]}/feedback", json={"label": "not_mine"}, headers=h).status_code == 404
