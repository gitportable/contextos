from datetime import timedelta

from app import assistant, integrations
from app.models import MessageProject, Project, SlackConnection, SlackMessage, User, utcnow


def _messages(db, user):
    connection = SlackConnection(owner_id=user.id, team_id="SUMMARY", slack_user_id="USUMMARY", status="connected")
    db.add(connection)
    db.flush()
    messages = [
        SlackMessage(owner_id=user.id, connection_id=connection.id, team_id=connection.team_id,
                     channel_id="C1", slack_ts="1.0", author_name="Ana Ruiz",
                     text="Atlas pricing interview results support fewer tiers: seven of nine customers prefer them.",
                     priority_label="P1", priority_score=80, message_created_at=utcnow() - timedelta(hours=2)),
        SlackMessage(owner_id=user.id, connection_id=connection.id, team_id=connection.team_id,
                     channel_id="C1", slack_ts="2.0", author_name="Lena Fischer",
                     text="@You (demo) can you review the Atlas pricing tiers before launch?",
                     priority_label="P2", priority_score=50, message_created_at=utcnow() - timedelta(hours=1)),
        SlackMessage(owner_id=user.id, connection_id=connection.id, team_id=connection.team_id,
                     channel_id="C1", slack_ts="3.0", author_name="Priya Nair",
                     text="Phoenix index rebuild is blocked while we wait on a decision.",
                     priority_label="P0", priority_score=100, message_created_at=utcnow()),
    ]
    db.add_all(messages)
    db.commit()
    return messages


def test_summary_request_uses_high_signal_context_and_synthesizes_fallback(db, user, monkeypatch):
    monkeypatch.setattr(integrations, "search_vectors", lambda *args, **kwargs: [])
    monkeypatch.setattr(assistant.llm, "enabled", lambda: False)
    messages = _messages(db, user)

    result = assistant.ask(db, user, "Summarise the important updates")

    assert result["mode"] == "extractive"
    assert result["answer"].startswith("Here are the main updates:")
    assert "A few things stand out" not in result["answer"]
    assert "Atlas pricing interview results" in result["answer"]
    assert "review the Atlas pricing tiers" in result["answer"]
    assert "seven of nine customers prefer them" in result["answer"]
    assert len(result["sources"]) == 3
    assert {source["message_id"] for source in result["sources"]} == {message.id for message in messages}


def test_llm_summary_prompt_requests_synthesis_and_removes_null_artifact(db, user, monkeypatch):
    monkeypatch.setattr(integrations, "search_vectors", lambda *args, **kwargs: [])
    monkeypatch.setattr(assistant.llm, "enabled", lambda: True)
    messages = _messages(db, user)
    prompts = []

    def fake_chat(_db, _owner_id, prompt, **_kwargs):
        prompts.extend(prompt)
        return "Atlas feedback favors fewer pricing tiers. nullnull"

    monkeypatch.setattr(assistant.llm, "chat", fake_chat)
    result = assistant.ask(db, user, "Summarise the important updates")

    assert result["mode"] == "llm"
    assert result["answer"] == "Atlas feedback favors fewer pricing tiers."
    assert "synthesize and group the main updates" in prompts[0]["content"]
    assert "important updates" not in result["answer"].lower()


def test_provider_unavailable_explains_fallback_without_losing_answer(db, user, monkeypatch):
    monkeypatch.setattr(integrations, "search_vectors", lambda *args, **kwargs: [])
    monkeypatch.setattr(assistant.llm, "enabled", lambda: True)
    _messages(db, user)

    def unavailable(*_args, **_kwargs):
        raise assistant.llm.LLMUnavailable("LLM provider returned HTTP 401")

    monkeypatch.setattr(assistant.llm, "chat", unavailable)
    result = assistant.ask(db, user, "Summarise the important updates")

    assert result["mode"] == "extractive"
    assert result["answer"].startswith("Here are the main updates:")
    assert "HTTP 401" in result["note"]
    assert "showing the retrieved context" in result["note"]


def test_greeting_is_not_labeled_as_an_llm_fallback(db, user):
    result = assistant.ask(db, user, "sup")

    assert result["mode"] == "extractive"
    assert result["note"] == ""
    assert result["answer"].startswith("Hi!")


def test_null_only_llm_output_returns_clean_extractive_answer(db, user, monkeypatch):
    monkeypatch.setattr(integrations, "search_vectors", lambda *args, **kwargs: [])
    monkeypatch.setattr(assistant.llm, "enabled", lambda: True)
    _messages(db, user)
    monkeypatch.setattr(assistant.llm, "chat", lambda *_args, **_kwargs: "nullnullnull")

    result = assistant.ask(db, user, "Summarise the important updates")

    assert result["mode"] == "extractive"
    assert "null" not in result["answer"].lower()
    assert result["answer"].startswith("Here are the main updates:")


def test_anything_important_uses_summary_retrieval_and_fallback(db, user, monkeypatch):
    monkeypatch.setattr(integrations, "search_vectors", lambda *args, **kwargs: [])
    monkeypatch.setattr(assistant.llm, "enabled", lambda: False)
    _messages(db, user)

    result = assistant.ask(db, user, "anything important")

    assert result["mode"] == "extractive"
    assert result["answer"].startswith("Here are the main updates:")
    assert "A few things stand out" not in result["answer"]


def test_semantic_hits_are_reloaded_with_owner_and_project_filters(db, user, monkeypatch):
    messages = _messages(db, user)
    project = Project(owner_id=user.id, name="Atlas pricing", slug="atlas-pricing", status="confirmed")
    db.add(project)
    db.flush()
    db.add(MessageProject(message_id=messages[0].id, project_id=project.id, owner_id=user.id))
    semantic_match = SlackMessage(
        owner_id=user.id, connection_id=messages[0].connection_id, team_id=messages[0].team_id,
        channel_id="C1", slack_ts="4.0", author_name="Ana Ruiz", text="Pricing rollout risk review.",
        priority_label="P3", priority_score=10, message_created_at=utcnow(),
    )
    other_owner = User(email="other@example.com", name="Other", password_hash="x")
    db.add(other_owner)
    db.flush()
    other_connection = SlackConnection(
        owner_id=other_owner.id, team_id="OTHER", slack_user_id="UOTHER", status="connected"
    )
    db.add(other_connection)
    db.flush()
    foreign_message = SlackMessage(
        owner_id=other_owner.id, connection_id=other_connection.id, team_id="OTHER",
        channel_id="C2", slack_ts="5.0", text="Private message from another account.",
        priority_label="P1", priority_score=80, message_created_at=utcnow(),
    )
    db.add_all([semantic_match, foreign_message])
    db.flush()
    db.add(MessageProject(message_id=semantic_match.id, project_id=project.id, owner_id=user.id))
    db.commit()
    monkeypatch.setattr(integrations, "search_vectors", lambda *args, **kwargs: [
        {"message_id": semantic_match.id},
        {"message_id": messages[1].id},
        {"message_id": foreign_message.id},
        {"message_id": 999999},
    ])

    summary_messages = assistant._summary_messages(db, user.id, "Summarise Atlas pricing", [project.id])
    ranked_messages = assistant._rank_messages(db, user.id, "unmatched query", [project.id])

    expected_ids = {messages[0].id, semantic_match.id}
    assert {message.id for message in summary_messages} == expected_ids
    assert {message.id for message in ranked_messages} == expected_ids
