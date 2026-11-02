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
                    channel_id="inbox", slack_ts=f"{index}-{score}", text=f"{priority} score {score}",
                    priority_label=priority, priority_score=score,
                ))
        db.commit()

    important_response = client.get("/messages", params={"priority": "important", "source": "gmail"})
    assert important_response.status_code == 200, important_response.text
    important = important_response.json()
    assert [message["priority_label"] for message in important] == ["P0", "P1", "P2", "P3"]
    assert [message["priority_score"] for message in important] == [90, 90, 90, 90]

    urgent = client.get("/messages", params={"priority": "urgent", "source": "gmail"}).json()
    assert {message["priority_label"] for message in urgent} == {"P0", "P1"}
    assert len(urgent) == 4

    high = client.get("/messages", params={"priority": "high", "source": "gmail"}).json()
    assert {message["priority_label"] for message in high} == {"P1"}
    normal = client.get("/messages", params={"priority": "normal", "source": "gmail"}).json()
    assert {message["priority_label"] for message in normal} == {"P2", "P3"}


def test_security_headers_and_unauthenticated_api(client):
    r = client.get("/api/config")
    assert "script-src 'self'" in r.headers["content-security-policy"] and r.headers["x-frame-options"] == "DENY"
    assert client.get("/messages").status_code == 401


def test_owner_only_admin_and_pipeline_routes(client, monkeypatch):
    from app.config import settings
    import app.routes_app as routes_app
    monkeypatch.setattr(settings, "admin_email", "owner@example.com")
    monkeypatch.setattr(routes_app, "service_status", lambda: {
        "redis": ("off", "not configured"), "qdrant": ("off", "not configured"), "neo4j": ("off", "not configured")
    })
    admin, _ = register(client, "owner@example.com")
    assert admin.status_code == 200
    assert client.get("/api/admin/settings").status_code == 200
    assert client.get("/api/pipeline/overview").status_code == 200
    client.cookies.clear()
    register(client, "member@example.com")
    assert client.get("/api/admin/settings").status_code == 403
    assert client.get("/api/pipeline/overview").status_code == 403
    assert client.get("/api/pipeline/events").status_code == 403


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
                               duration_minutes=30, status="suggested", assigned_people="[]")
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
                              status="suggested", assigned_people="[]"))
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
    assert "One thing that may help:" in result["answer"]
    assert "[#" not in result["answer"]
    assert result["sources"] and "NebulaXenon" in result["sources"][0]["text"]
    greeting = client.post("/assistant/ask", json={"question": "sup"}, headers={"X-CSRF-Token": csrf})
    assert greeting.status_code == 200
    assert greeting.json()["answer"].startswith("Hi!")
    assert greeting.json()["sources"] == []


def test_assistant_treats_yo_as_greeting_without_searching_messages(client, monkeypatch):
    import app.integrations as integrations

    def unexpected_search(*args, **kwargs):
        raise AssertionError("A greeting must not trigger message retrieval.")

    monkeypatch.setattr(integrations, "search_vectors", unexpected_search)
    response, csrf = register(client, "assistant-yo@example.com")
    assert response.status_code == 200
    with client:
        response = client.post("/assistant/ask", json={"question": "yo"},
                               headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["answer"].startswith("Hi!")
    assert result["sources"] == []


def test_assistant_uses_recent_chat_context_for_follow_up_questions(client, monkeypatch):
    import app.database as database
    import app.integrations as integrations
    from app.models import SlackConnection, SlackMessage, User

    monkeypatch.setattr(integrations, "search_vectors", lambda *args, **kwargs: [])
    response, csrf = register(client, "assistant-follow-up@example.com")
    assert response.status_code == 200
    with database.SessionLocal() as db:
        user = db.query(User).filter_by(email="assistant-follow-up@example.com").one()
        connection = SlackConnection(owner_id=user.id, team_id="FOLLOWUP", slack_user_id="UFOLLOWUP",
                                     status="connected")
        db.add(connection)
        db.flush()
        phoenix = SlackMessage(owner_id=user.id, connection_id=connection.id, team_id=connection.team_id,
                               channel_id="C1", slack_ts="1.0", author_name="Dev Patel",
                               text="Phoenix migration is blocked on the DNS fix.",
                               priority_label="P1", priority_score=30)
        atlas = SlackMessage(owner_id=user.id, connection_id=connection.id, team_id=connection.team_id,
                             channel_id="C1", slack_ts="2.0", author_name="Omar Haddad",
                             text="Atlas pricing approval is due today.",
                             priority_label="P0", priority_score=100)
        db.add_all([phoenix, atlas])
        db.commit()
        phoenix_id = phoenix.id

    response = client.post("/assistant/ask", json={
        "question": "what about that?",
        "history": [
            {"role": "user", "content": "What is happening with Phoenix?"},
            {"role": "assistant", "content": "Dev says the Phoenix migration is blocked on DNS."},
        ],
    }, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["sources"]
    assert result["sources"][0]["message_id"] == phoenix_id
    assert "Phoenix migration" in result["answer"]


def test_assistant_drafts_reply_to_named_slack_person_without_sending(client, monkeypatch):
    import app.database as database
    import app.integrations as integrations
    from app.models import OutboundAction, SlackConnection, SlackConversation, SlackMessage, User

    monkeypatch.setattr(integrations, "search_vectors", lambda *args, **kwargs: [])
    response, csrf = register(client, "assistant-reply-draft@example.com")
    assert response.status_code == 200
    with database.SessionLocal() as db:
        user = db.query(User).filter_by(email="assistant-reply-draft@example.com").one()
        connection = SlackConnection(owner_id=user.id, team_id="DRAFT", slack_user_id="UOWNER",
                                     status="connected", provider="slack", granted_user_scopes="chat:write")
        db.add(connection)
        db.flush()
        db.add(SlackConversation(connection_id=connection.id, channel_id="C1", name="platform",
                                 conv_type="public_channel", is_active=True))
        message = SlackMessage(owner_id=user.id, connection_id=connection.id, team_id=connection.team_id,
                               channel_id="C1", slack_ts="1.0", user_id="UDEV", author_name="Dev Patel",
                               text="Phoenix post-migration checklist drafted, please add anything I missed.",
                               priority_label="P2", priority_score=20)
        db.add(message)
        db.commit()
        message_id = message.id

    response = client.post("/assistant/ask", json={"question": "can u telll dev to wait for a meet on tuesday"},
                           headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["answer"] == "I drafted a short message for Dev Patel. Choose a channel and review it before posting."
    assert result["suggested_reply"]["recipient"] == "Dev Patel"
    assert result["suggested_reply"]["draft"] == "Could you hold off until Tuesday? Let's meet then, and I'll confirm a time shortly."
    assert result["suggested_reply"]["channel_options"] == [{
        "connection_id": connection.id,
        "channel_id": "C1",
        "channel_name": "platform",
        "can_post": True,
        "about_recipient": True,
    }]
    assert result["sources"][0]["message_id"] == message_id
    with database.SessionLocal() as db:
        assert db.query(OutboundAction).filter_by(owner_id=user.id).count() == 0


def test_channel_post_requires_confirmation_and_demo_post_is_simulated(client):
    import app.database as database
    from app.models import OutboundAction, SlackConnection, SlackConversation, User

    response, csrf = register(client, "channel-post@example.com")
    assert response.status_code == 200
    with database.SessionLocal() as db:
        user = db.query(User).filter_by(email="channel-post@example.com").one()
        connection = SlackConnection(owner_id=user.id, team_id="POST", slack_user_id="UPOST",
                                     status="connected", provider="slack", is_demo=True)
        db.add(connection)
        db.flush()
        db.add(SlackConversation(connection_id=connection.id, channel_id="CPOST", name="general",
                                 conv_type="public_channel", is_active=True))
        db.commit()
        connection_id = connection.id

    path = f"/channels/{connection_id}/CPOST/messages"
    response = client.post(path, json={"text": "Dev, could you schedule a meeting?"},
                           headers={"X-CSRF-Token": csrf})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "approval_required"
    response = client.post(path, json={"text": "Dev, could you schedule a meeting?", "approved": True,
                                       "client_request_id": "draft-post-1"},
                           headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200, response.text
    assert response.json()["simulated"] is True
    assert response.json()["type"] == "slack_channel_post"
    with database.SessionLocal() as db:
        action = db.query(OutboundAction).filter_by(idempotency_key="draft-post-1").one()
        assert action.status == "simulated"


def test_assistant_channel_composition_request_offers_a_composer(client, monkeypatch):
    import app.integrations as integrations

    monkeypatch.setattr(integrations, "search_vectors", lambda *args, **kwargs: [])
    response, csrf = register(client, "assistant-channel-compose@example.com")
    assert response.status_code == 200
    response = client.post("/assistant/ask", json={"question": "create a message to reply in one of the channels"},
                           headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["answer"].startswith("Sure. Choose a channel")
    assert result["channel_compose"] is True
    assert result["sources"] == []


def test_assistant_drafts_message_for_person_even_without_author_message(client):
    import app.database as database
    from app.models import SlackConnection, SlackConversation, User

    response, csrf = register(client, "assistant-reply-no-message@example.com")
    assert response.status_code == 200
    with database.SessionLocal() as db:
        user = db.query(User).filter_by(email="assistant-reply-no-message@example.com").one()
        connection = SlackConnection(owner_id=user.id, team_id="NOAUTHOR", slack_user_id="UOWNER",
                                     status="connected", provider="slack", is_demo=True)
        db.add(connection)
        db.flush()
        db.add(SlackConversation(connection_id=connection.id, channel_id="C1", name="general",
                                 conv_type="public_channel", is_active=True))
        db.commit()

    response = client.post("/assistant/ask", json={"question": "bruh tell dev to schedule a meet"},
                           headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["suggested_reply"]["draft"] == "Dev, could you schedule a meeting?"
    assert result["suggested_reply"]["channel_options"][0]["channel_id"] == "C1"
    assert result["sources"] == []


def test_assistant_today_question_only_returns_deadlines_due_today(client, monkeypatch):
    from datetime import datetime, timezone
    import app.database as database
    import app.integrations as integrations
    from app.models import SlackConnection, SlackMessage, User

    monkeypatch.setattr(integrations, "search_vectors", lambda *args, **kwargs: [])
    response, csrf = register(client, "assistant-today-focus@example.com")
    assert response.status_code == 200
    with database.SessionLocal() as db:
        user = db.query(User).filter_by(email="assistant-today-focus@example.com").one()
        connection = SlackConnection(owner_id=user.id, team_id="TODAY", slack_user_id="UTODAY", status="connected")
        db.add(connection)
        db.flush()
        db.add_all([
            SlackMessage(owner_id=user.id, connection_id=connection.id, team_id=connection.team_id,
                         channel_id="C1", slack_ts="1.0", author_name="Omar Haddad",
                         text="@You please approve the Q4 roadmap by 5pm today.",
                         priority_label="P1", priority_score=80, message_created_at=datetime.now(timezone.utc)),
            SlackMessage(owner_id=user.id, connection_id=connection.id, team_id=connection.team_id,
                         channel_id="C1", slack_ts="2.0", author_name="Dev Patel",
                         text="Phoenix post-migration checklist drafted, please add anything I missed.",
                         priority_label="P1", priority_score=70, message_created_at=datetime.now(timezone.utc)),
        ])
        db.commit()

    response = client.post("/assistant/ask", json={"question": "anything imp for today?"},
                           headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200, response.text
    result = response.json()
    assert "Omar Haddad" in result["answer"]
    assert "needs your approval on the Q4 roadmap" in result["answer"]
    assert "by 5pm today" in result["answer"]
    assert "Phoenix post-migration" not in result["answer"]
    assert len(result["sources"]) == 1
    assert "suggested_reply" not in result


def test_assistant_asks_which_person_when_first_name_is_ambiguous(client, monkeypatch):
    import app.database as database
    import app.integrations as integrations
    from app.models import SlackConnection, SlackMessage, User

    monkeypatch.setattr(integrations, "search_vectors", lambda *args, **kwargs: [])
    response, csrf = register(client, "assistant-reply-ambiguous@example.com")
    assert response.status_code == 200
    with database.SessionLocal() as db:
        user = db.query(User).filter_by(email="assistant-reply-ambiguous@example.com").one()
        connection = SlackConnection(owner_id=user.id, team_id="AMBIGUOUS", slack_user_id="UOWNER",
                                     status="connected", provider="slack")
        db.add(connection)
        db.flush()
        for author, user_id, timestamp in (("Dev Patel", "UDEV1", "1.0"), ("Dev Jones", "UDEV2", "2.0")):
            db.add(SlackMessage(owner_id=user.id, connection_id=connection.id, team_id=connection.team_id,
                                channel_id="C1", slack_ts=timestamp, user_id=user_id, author_name=author,
                                text="A message from " + author, priority_label="P2", priority_score=20))
        db.commit()

    response = client.post("/assistant/ask", json={"question": "tell Dev to wait for Tuesday"},
                           headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["answer"] == "I found more than one person with that name. Which one did you mean: Dev Jones, Dev Patel?"
    assert "suggested_reply" not in result
    assert result["sources"] == []


def test_assistant_stream_sends_tokens_before_final_sources(client, monkeypatch):
    import app.assistant as assistant
    import app.database as database
    import app.integrations as integrations
    from app.models import SlackConnection, SlackMessage, User

    monkeypatch.setattr(integrations, "search_vectors", lambda *args, **kwargs: [])
    monkeypatch.setattr(assistant.llm, "enabled", lambda: True)
    _, csrf = register(client, "assistant-stream@example.com")
    with database.SessionLocal() as db:
        user = db.query(User).filter_by(email="assistant-stream@example.com").one()
        connection = SlackConnection(owner_id=user.id, team_id="STREAM", slack_user_id="USTREAM", status="connected")
        db.add(connection)
        db.flush()
        message = SlackMessage(owner_id=user.id, connection_id=connection.id, team_id=connection.team_id,
                               channel_id="C1", slack_ts="1.0", text="NebulaXenon release review is blocked until Friday.",
                               priority_label="P1", priority_score=80)
        db.add(message)
        db.commit()
        message_id = message.id

    def fake_stream(_db, _owner_id, prompt, on_token, **_kwargs):
        assert "<conversation>" in prompt[1]["content"]
        assert "Tell me about the NebulaXenon release review." in prompt[1]["content"]
        on_token("The review is blocked ")
        on_token(f"[#{message_id}]")
        return f"The review is blocked [#{message_id}]"

    monkeypatch.setattr(assistant.llm, "chat_stream", fake_stream)
    response = client.post("/assistant/ask/stream", json={
        "question": "What about NebulaXenon?",
        "history": [{"role": "user", "content": "Tell me about the NebulaXenon release review."}],
    },
                           headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200
    assert "event: token" in response.text
    assert "The review is blocked " in response.text
    assert "event: done" in response.text
    assert f'"message_id": {message_id}' in response.text


def test_inbox_and_dashboard_include_own_slack_messages(client):
    import app.database as database
    from app.models import SlackConnection, SlackMessage, User

    _, csrf = register(client, "own-message@example.com")
    with database.SessionLocal() as db:
        user = db.query(User).filter_by(email="own-message@example.com").one()
        connection = SlackConnection(owner_id=user.id, team_id="OWN", slack_user_id="UOWN", status="connected")
        db.add(connection)
        db.flush()
        db.add(SlackMessage(owner_id=user.id, connection_id=connection.id, team_id=connection.team_id,
                            channel_id="C1", slack_ts="1.0", user_id="UOWN", is_self=True,
                            text="Message I sent to a colleague", priority_label="P3"))
        db.commit()

    messages = client.get("/messages?include_self=true")
    assert messages.status_code == 200
    assert len(messages.json()) == 1 and messages.json()[0]["is_self"]
    search = client.get("/api/search?q=colleague")
    assert search.status_code == 200 and search.json()["results"][0]["is_self"]
    summary = client.get("/dashboard/summary")
    assert summary.status_code == 200 and summary.json()["messages_total"] == 1


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
    assert "meeting-related item" in result["answer"]
    assert "ProjectOrion planning call" in result["answer"]
    assert "[#" not in result["answer"]
    assert result["sources"][0]["message_id"] == message.id


def test_assistant_summarizes_meeting_details_instead_of_dumping_messages(client, monkeypatch):
    import app.assistant as assistant
    import app.database as database
    import app.integrations as integrations
    from app.models import SlackConnection, SlackMessage, User

    monkeypatch.setattr(assistant.llm, "enabled", lambda: False)
    monkeypatch.setattr(integrations, "search_vectors", lambda *args, **kwargs: [])
    response, csrf = register(client, "meeting-details@example.com")
    assert response.status_code == 200
    with database.SessionLocal() as db:
        user = db.query(User).filter_by(email="meeting-details@example.com").one()
        connection = SlackConnection(owner_id=user.id, team_id="MEETING-DETAILS", slack_user_id="UMEETING", status="connected")
        db.add(connection)
        db.flush()
        text = ("78catalyst - Cohort Session5 Wednesday, October 21 · 8:00 – 9:00pm "
                "Time zone: Asia/Kolkata Google Meet joining info Video call link: meet.google.com/ymt-deyz-odr")
        messages = [
            SlackMessage(owner_id=user.id, connection_id=connection.id, team_id=connection.team_id,
                         channel_id="C1", slack_ts=f"{i}.0", text=text, priority_label="P1", priority_score=80)
            for i in range(2)
        ]
        db.add_all(messages)
        db.add(SlackMessage(owner_id=user.id, connection_id=connection.id, team_id=connection.team_id,
                            channel_id="C1", slack_ts="3.0", text="Please review the roadmap before the board meeting.",
                            priority_label="P1", priority_score=80))
        db.commit()

    answer = client.post("/assistant/ask", json={"question": "whats the meet"},
                         headers={"X-CSRF-Token": csrf})
    assert answer.status_code == 200, answer.text
    result = answer.json()
    assert "Cohort Session5 Google Meet" in result["answer"]
    assert "Wednesday, October 21" in result["answer"]
    assert "8:00 – 9:00pm" in result["answer"]
    assert "Asia/Kolkata" in result["answer"]
    assert "roadmap" not in result["answer"]
    assert "[#" not in result["answer"]
    assert len(result["sources"]) == 1
    assert "Cohort Session5" in result["sources"][0]["text"]


def test_assistant_summarizes_important_meet_query_and_deduplicates_sources(client, monkeypatch):
    import app.assistant as assistant
    import app.database as database
    import app.integrations as integrations
    from app.models import SlackConnection, SlackMessage, User

    monkeypatch.setattr(assistant.llm, "enabled", lambda: False)
    monkeypatch.setattr(integrations, "search_vectors", lambda *args, **kwargs: [])
    response, csrf = register(client, "important-meeting@example.com")
    assert response.status_code == 200
    with database.SessionLocal() as db:
        user = db.query(User).filter_by(email="important-meeting@example.com").one()
        connection = SlackConnection(owner_id=user.id, team_id="IMPORTANT-MEETING", slack_user_id="UIMPORTANT", status="connected")
        db.add(connection)
        db.flush()
        text = ("78catalyst - Cohort Session5 Wednesday, October 21 · 8:00 – 9:00pm "
                "Time zone: Asia/Kolkata Google Meet joining info Video call link: meet.google.com/ymt-deyz-odr")
        db.add_all([
            SlackMessage(owner_id=user.id, connection_id=connection.id, team_id=connection.team_id,
                         channel_id="C1", slack_ts=f"{i}.0", text=text, priority_label="P1", priority_score=80)
            for i in range(2)
        ])
        db.commit()

    result = client.post("/assistant/ask", json={"question": "hmm any important meet ?"},
                         headers={"X-CSRF-Token": csrf})
    assert result.status_code == 200, result.text
    payload = result.json()
    assert "Cohort Session5 Google Meet" in payload["answer"]
    assert "That date has passed" in payload["answer"]
    assert "A few things stand out" not in payload["answer"]
    assert len(payload["sources"]) == 1


def test_assistant_greeting_stream_never_serializes_missing_answer_as_null(client):
    response, csrf = register(client, "assistant-greeting-stream@example.com")
    assert response.status_code == 200
    streamed = client.post("/assistant/ask/stream", json={"question": "sup"},
                           headers={"X-CSRF-Token": csrf})
    assert streamed.status_code == 200, streamed.text
    assert '"answer": "Hi!' in streamed.text
    assert "null" not in streamed.text


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
