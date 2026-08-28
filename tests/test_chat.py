import asyncio
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.agents import action_parser as action_parser_module
from app.agents import narrator as narrator_module
from app.api.dependencies import INTERNAL_USER_ID_HEADER_NAME
from app.core.config import settings
from app.db.session import session
from app.guardrails.model_policy import ModelPolicy
from app.main import app
from app.orchestration import orchestrator as orchestrator_module
from app.schemas.chat import (
    ActionParserOutput,
    ActionParserParameters,
    ActionType,
    ChatRequest,
)
from app.schemas.internal_auth import CANONICAL_GOOGLE_ISSUER


def _user_scoped_headers(
    client: TestClient, provider_subject: str = "chat-test-user"
) -> dict[str, str]:
    resolve_response = client.post(
        "/internal/auth/users/resolve",
        json={
            "identity_provider": "google",
            "provider_issuer": CANONICAL_GOOGLE_ISSUER,
            "provider_subject": provider_subject,
            "email": f"{provider_subject}@example.com",
            "email_verified": True,
            "display_name": "Test Player",
            "avatar_url": "https://example.com/avatar.png",
        },
        headers={
            "Authorization": f"Bearer {settings.INTERNAL_ENGINE_SERVICE_TOKEN or ''}"
        },
    )
    assert resolve_response.status_code == 200
    user_id = resolve_response.json()["user_id"]
    return {
        "Authorization": "Bearer test-token",
        INTERNAL_USER_ID_HEADER_NAME: user_id,
    }


def _resolved_internal_user_id(client: TestClient, provider_subject: str) -> str:
    resolve_response = client.post(
        "/internal/auth/users/resolve",
        json={
            "identity_provider": "google",
            "provider_issuer": CANONICAL_GOOGLE_ISSUER,
            "provider_subject": provider_subject,
            "email": f"{provider_subject}@example.com",
            "email_verified": True,
            "display_name": "Test Player",
            "avatar_url": "https://example.com/avatar.png",
        },
        headers={
            "Authorization": f"Bearer {settings.INTERNAL_ENGINE_SERVICE_TOKEN or ''}"
        },
    )
    assert resolve_response.status_code == 200
    return resolve_response.json()["user_id"]


def test_chat_echoes_message() -> None:
    settings.INTERNAL_ENGINE_SERVICE_TOKEN = "test-token"
    settings.AI_ENABLED = False
    settings.OPENAI_API_KEY = None
    client = TestClient(app)
    headers = _user_scoped_headers(client, "chat-echo")

    response = client.post(
        "/api/chat",
        json={"message": "hello"},
        headers=headers,
    )

    assert response.status_code == 200
    data = response.json()

    assert data["reply"] == "AI narrator replies (stub): hello"
    assert data["campaign_id"].startswith("campaign_")
    assert data["turn_id"].startswith("turn_")


def test_chat_requires_authorization() -> None:
    client = TestClient(app)

    response = client.post("/api/chat", json={"message": "hello"})

    assert response.status_code == 401


def test_campaign_routes_list_without_legacy_identity_param() -> None:
    settings.INTERNAL_ENGINE_SERVICE_TOKEN = "test-token"
    settings.AI_ENABLED = False
    settings.OPENAI_API_KEY = None
    client = TestClient(app)
    headers = _user_scoped_headers(client, "campaign-anonymous")

    response = client.get("/api/campaigns", headers=headers)

    assert response.status_code == 200


def test_create_campaign_returns_hydrated_campaign() -> None:
    settings.INTERNAL_ENGINE_SERVICE_TOKEN = "test-token"
    settings.AI_ENABLED = False
    settings.OPENAI_API_KEY = None
    client = TestClient(app)
    headers = _user_scoped_headers(client, "campaign-create")

    response = client.post(
        "/api/campaign",
        json={},
        headers=headers,
    )

    assert response.status_code == 201
    data = response.json()

    assert data["campaign_id"].startswith("campaign_")
    assert data["name"] == "The Bell Beneath the Hall"
    assert data["truncated"] is False
    assert len(data["messages"]) == 1
    assert data["messages"][0]["role"] == "assistant"
    assert "What do you do first?" in data["messages"][0]["content"]


def test_create_campaign_uses_narrator_for_opening_and_title(monkeypatch) -> None:
    settings.INTERNAL_ENGINE_SERVICE_TOKEN = "test-token"
    settings.AI_ENABLED = True
    settings.OPENAI_API_KEY = None
    prompts: list[str] = []

    async def fake_generate_text(*, messages, **kwargs) -> str:
        prompts.append(messages[-1]["content"])
        if (
            "provide only a short haunted campaign title"
            in messages[-1]["content"].lower()
        ):
            return "Whispers in the West Wing"
        return "The candles hiss awake in the corridor. Where do you step first?"

    monkeypatch.setattr(
        narrator_module.model_client, "generate_text", fake_generate_text
    )
    client = TestClient(app)
    headers = _user_scoped_headers(client, "campaign-narrator")

    response = client.post(
        "/api/campaign",
        json={},
        headers=headers,
    )

    assert response.status_code == 201
    data = response.json()

    assert len(prompts) == 2
    assert "Start a new haunted halls campaign" in prompts[0]
    assert data["name"] == "Whispers in the West Wing"
    assert (
        data["messages"][0]["content"]
        == "The candles hiss awake in the corridor. Where do you step first?"
    )


def test_delete_campaign_removes_player_campaign() -> None:
    settings.INTERNAL_ENGINE_SERVICE_TOKEN = "test-token"
    settings.AI_ENABLED = False
    settings.OPENAI_API_KEY = None
    client = TestClient(app)
    headers = _user_scoped_headers(client, "campaign-delete")

    create_response = client.post(
        "/api/campaign",
        json={},
        headers=headers,
    )
    assert create_response.status_code == 201
    campaign_id = create_response.json()["campaign_id"]

    delete_response = client.delete(
        f"/api/campaign/{campaign_id}",
        headers=headers,
    )

    assert delete_response.status_code == 204

    get_response = client.get(
        f"/api/campaign/{campaign_id}",
        headers=headers,
    )
    assert get_response.status_code == 404


def test_delete_campaign_returns_404_for_missing_or_unowned_campaign() -> None:
    settings.INTERNAL_ENGINE_SERVICE_TOKEN = "test-token"
    settings.AI_ENABLED = False
    settings.OPENAI_API_KEY = None
    client = TestClient(app)
    headers = _user_scoped_headers(client, "campaign-missing")

    response = client.delete(
        "/api/campaign/campaign_missing",
        headers=headers,
    )

    assert response.status_code == 404


def test_delete_campaign_requires_authorization() -> None:
    settings.AI_ENABLED = False
    settings.OPENAI_API_KEY = None
    client = TestClient(app)

    response = client.delete(
        "/api/campaign/campaign_any",
    )

    assert response.status_code == 401


def test_chat_daily_request_limit_rejects_before_persisting_side_effects() -> None:
    settings.INTERNAL_ENGINE_SERVICE_TOKEN = "test-token"
    settings.AI_ENABLED = False
    settings.OPENAI_API_KEY = None
    original_limit = settings.MAX_DAILY_PLAYER_REQUESTS
    settings.MAX_DAILY_PLAYER_REQUESTS = 0
    client = TestClient(app)
    headers = _user_scoped_headers(client, "chat-limit-no-write")

    try:
        with session() as db:
            counts_before = {
                "campaigns": int(
                    db.conn.execute("SELECT COUNT(*) AS total FROM campaigns").fetchone()["total"]
                ),
                "turns": int(db.conn.execute("SELECT COUNT(*) AS total FROM turns").fetchone()["total"]),
                "events": int(
                    db.conn.execute("SELECT COUNT(*) AS total FROM game_events").fetchone()["total"]
                ),
                "requests": int(
                    db.conn.execute("SELECT COUNT(*) AS total FROM model_requests").fetchone()["total"]
                ),
            }

        response = client.post(
            "/api/chat",
            json={"message": "hello"},
            headers=headers,
        )

        assert response.status_code == 429
        error = response.json()["detail"]
        assert error["code"] == "daily_request_limit"
        assert error["retryable"] is False
        assert error["retry_at"].endswith("T00:00:00Z")

        with session() as db:
            counts_after = {
                "campaigns": int(
                    db.conn.execute("SELECT COUNT(*) AS total FROM campaigns").fetchone()["total"]
                ),
                "turns": int(db.conn.execute("SELECT COUNT(*) AS total FROM turns").fetchone()["total"]),
                "events": int(
                    db.conn.execute("SELECT COUNT(*) AS total FROM game_events").fetchone()["total"]
                ),
                "requests": int(
                    db.conn.execute("SELECT COUNT(*) AS total FROM model_requests").fetchone()["total"]
                ),
            }

        assert counts_after == counts_before
    finally:
        settings.MAX_DAILY_PLAYER_REQUESTS = original_limit


def test_chat_daily_token_limit_returns_structured_error() -> None:
    settings.INTERNAL_ENGINE_SERVICE_TOKEN = "test-token"
    settings.AI_ENABLED = False
    settings.OPENAI_API_KEY = None
    original_limit = settings.MAX_DAILY_PLAYER_TOKENS
    settings.MAX_DAILY_PLAYER_TOKENS = 0
    client = TestClient(app)
    headers = _user_scoped_headers(client, "chat-token-limit")

    try:
        response = client.post(
            "/api/chat",
            json={"message": "hello"},
            headers=headers,
        )

        assert response.status_code == 429
        error = response.json()["detail"]
        assert error == {
            "detail": "Daily token limit reached.",
            "code": "daily_token_limit",
            "retryable": False,
            "retry_at": error["retry_at"],
        }
        assert error["retry_at"].endswith("T00:00:00Z")
    finally:
        settings.MAX_DAILY_PLAYER_TOKENS = original_limit


def test_chat_campaign_turn_limit_returns_structured_error(monkeypatch) -> None:
    settings.INTERNAL_ENGINE_SERVICE_TOKEN = "test-token"
    settings.AI_ENABLED = False
    settings.OPENAI_API_KEY = None
    monkeypatch.setattr(settings, "MAX_TURNS_PER_CAMPAIGN", 0)
    client = TestClient(app)
    headers = _user_scoped_headers(client, "chat-turn-limit")
    campaign_response = client.post("/api/campaign", json={}, headers=headers)
    campaign_id = campaign_response.json()["campaign_id"]

    response = client.post(
        "/api/chat",
        json={"message": "hello", "campaign_id": campaign_id},
        headers=headers,
    )

    assert response.status_code == 429
    assert response.json()["detail"] == {
        "detail": "This campaign has reached its turn limit.",
        "code": "campaign_turn_limit",
        "retryable": False,
    }


def test_create_campaign_limit_returns_structured_error(monkeypatch) -> None:
    settings.INTERNAL_ENGINE_SERVICE_TOKEN = "test-token"
    settings.AI_ENABLED = False
    settings.OPENAI_API_KEY = None
    monkeypatch.setattr(settings, "MAX_CAMPAIGNS_PER_PLAYER", 0)
    monkeypatch.setattr("app.guardrails.usage_limits.UsageLimits.MAX_CAMPAIGNS_PER_PLAYER", 0)
    client = TestClient(app)
    headers = _user_scoped_headers(client, "campaign-count-limit")

    response = client.post("/api/campaign", json={}, headers=headers)

    assert response.status_code == 429
    assert response.json()["detail"] == {
        "detail": "Maximum number of campaigns reached.",
        "code": "max_campaigns",
        "retryable": False,
    }


def test_orchestrator_uses_narrator_agent_and_persists_turn(monkeypatch) -> None:
    settings.AI_ENABLED = True
    settings.OPENAI_API_KEY = "test-key"

    saw_tool_context = False

    async def fake_generate_structured(*, messages, model=None, **kwargs):  # noqa: ANN202, ARG001
        assert messages[0]["role"] == "developer"
        return ActionParserOutput(
            action=ActionType.MOVE,
            target="north",
            parameters=ActionParserParameters(),
            stealth=True,
            confidence=0.94,
            parse_status="ok",
            parser_notes=None,
        )

    async def fake_generate_text(*, messages, **kwargs) -> str:
        nonlocal saw_tool_context
        assert messages[0]["role"] == "developer"

        assert messages[1]["role"] == "user"
        assert "Campaign state:" in messages[1]["content"]
        assert any(
            "Tool execution result:" in str(message["content"]) for message in messages
        )
        saw_tool_context = True
        assert messages[-1]["role"] == "user"
        assert messages[-1]["content"] == "hello"
        return "A haunted reply"

    monkeypatch.setattr(
        action_parser_module.model_client,
        "generate_structured",
        fake_generate_structured,
    )
    monkeypatch.setattr(
        narrator_module.model_client, "generate_text", fake_generate_text
    )

    response = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="hello"),
            owner_user_id=_resolved_internal_user_id(
                TestClient(app), "orchestrator-chat"
            ),
        )
    )

    assert response.reply == "A haunted reply"
    assert saw_tool_context is True

    with session() as db:
        campaign = db.get_campaign(response.campaign_id)
        assert campaign is not None
        assert campaign.state is not None
        campaign_state = json.loads(campaign.state)
        assert campaign_state["player"]["location"] == "grand_corridor"

        _, turns, _ = db.get_campaign_with_turns(response.campaign_id, limit=10)
        assert len(turns) >= 2
        assert turns[-2].role == "user"
        assert turns[-2].content == "hello"
        assert turns[-1].role == "assistant"
        assert turns[-1].content == "A haunted reply"

        events = db.list_campaign_events(response.campaign_id)
        assert [event.type for event in events[-5:]] == [
            "player_message_received",
            "action_parsed",
            "tool_executed",
            "game_state_updated",
            "narrator_response_created",
        ]
        assert events[-5].turn_id == turns[-2].turn_id
        assert events[-4].turn_id == turns[-2].turn_id
        assert events[-3].turn_id == turns[-2].turn_id
        assert events[-2].turn_id == turns[-2].turn_id
        assert events[-1].turn_id == turns[-1].turn_id
        assert json.loads(events[-5].payload_json or "{}") == {"message": "hello"}
        assert json.loads(events[-1].payload_json or "{}") == {
            "reply": "A haunted reply"
        }


def test_orchestrator_records_parse_failure_for_invalid_action(monkeypatch) -> None:
    settings.AI_ENABLED = True
    settings.OPENAI_API_KEY = "test-key"

    async def fake_generate_structured(*, messages, model=None, **kwargs):  # noqa: ANN202, ARG001
        return ActionParserOutput(
            action=ActionType.UNKNOWN,
            target=None,
            parameters=ActionParserParameters(),
            stealth=False,
            confidence=0.2,
            parse_status="invalid",
            parser_notes="Could not determine action",
        )

    async def fake_generate_text(*, messages, **kwargs) -> str:  # noqa: ARG001
        return "I cannot decode your intent, but the hall waits."

    monkeypatch.setattr(
        action_parser_module.model_client,
        "generate_structured",
        fake_generate_structured,
    )
    monkeypatch.setattr(
        narrator_module.model_client, "generate_text", fake_generate_text
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            orchestrator_module.orchestrator.handle_chat(
                ChatRequest(message="???"),
                owner_user_id=_resolved_internal_user_id(
                    TestClient(app), "orchestrator-invalid-action"
                ),
            )
        )

    assert exc_info.value.status_code == 422
    assert "Unprocessable action" in str(exc_info.value.detail)

    with session() as db:
        campaign_count = db.count_owner_campaigns(
            _resolved_internal_user_id(TestClient(app), "orchestrator-invalid-action")
        )
        assert campaign_count == 0


def test_orchestrator_returns_502_when_action_parser_provider_fails(
    monkeypatch,
) -> None:
    settings.AI_ENABLED = True
    settings.OPENAI_API_KEY = "test-key"

    async def broken_generate_structured(*, messages, **kwargs):  # noqa: ANN202, ARG001
        raise RuntimeError("parser upstream failure")

    monkeypatch.setattr(
        action_parser_module.model_client,
        "generate_structured",
        broken_generate_structured,
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            orchestrator_module.orchestrator.handle_chat(
                ChatRequest(message="move north"),
                owner_user_id=_resolved_internal_user_id(
                    TestClient(app), "orchestrator-parser-fail"
                ),
            )
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "Action parser service failed."


def test_orchestrator_persists_genesis_state_for_non_mutating_first_action(
    monkeypatch,
) -> None:
    settings.AI_ENABLED = True
    settings.OPENAI_API_KEY = None

    async def fake_generate_structured(*, messages, model=None, **kwargs):  # noqa: ANN202, ARG001
        return ActionParserOutput(
            action=ActionType.OBSERVE,
            target=None,
            parameters=ActionParserParameters(),
            stealth=False,
            confidence=0.9,
            parse_status="ok",
            parser_notes=None,
        )

    async def fake_generate_text(*, messages, **kwargs) -> str:  # noqa: ARG001
        return "You take stock of your surroundings."

    monkeypatch.setattr(
        action_parser_module.model_client,
        "generate_structured",
        fake_generate_structured,
    )
    monkeypatch.setattr(
        narrator_module.model_client, "generate_text", fake_generate_text
    )

    response = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="What items am I carrying?"),
            owner_user_id=_resolved_internal_user_id(
                TestClient(app), "orchestrator-genesis-observe"
            ),
        )
    )

    with session() as db:
        campaign = db.get_campaign(response.campaign_id)
        assert campaign is not None
        assert campaign.state is not None

        campaign_state = json.loads(campaign.state)
        assert "items" in campaign_state
        assert campaign_state["player"]["inventory"]


def test_ai_disabled_still_runs_parser_and_tools() -> None:
    settings.AI_ENABLED = False
    settings.OPENAI_API_KEY = None

    response = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="I go north."),
            owner_user_id=_resolved_internal_user_id(
                TestClient(app), "orchestrator-ai-disabled"
            ),
        )
    )

    assert (
        response.reply == "AI narrator replies (stub): I go north."
    )

    with session() as db:
        campaign = db.get_campaign(response.campaign_id)
        assert campaign is not None
        assert campaign.state is not None
        campaign_state = json.loads(campaign.state)
        assert campaign_state["player"]["location"] == "grand_corridor"

        events = db.list_campaign_events(response.campaign_id)
        assert [event.type for event in events[-5:]] == [
            "player_message_received",
            "action_parsed",
            "tool_executed",
            "game_state_updated",
            "narrator_response_created",
        ]


def test_ai_enabled_without_api_key_uses_deterministic_parser(monkeypatch) -> None:
    settings.AI_ENABLED = True
    settings.OPENAI_API_KEY = None

    async def fail_if_structured_called(*, messages, **kwargs):  # noqa: ANN202, ARG001
        raise AssertionError("generate_structured should not be called without API key")

    monkeypatch.setattr(
        action_parser_module.model_client,
        "generate_structured",
        fail_if_structured_called,
    )

    response = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="I go north."),
            owner_user_id=_resolved_internal_user_id(
                TestClient(app), "orchestrator-ai-flag-no-key"
            ),
        )
    )

    assert response.reply == "AI narrator replies: I go north."

    with session() as db:
        campaign = db.get_campaign(response.campaign_id)
        assert campaign is not None
        assert campaign.state is not None
        campaign_state = json.loads(campaign.state)
        assert campaign_state["player"]["location"] == "grand_corridor"


def test_orchestrator_includes_relevant_memory_context(monkeypatch) -> None:
    settings.AI_ENABLED = True
    settings.OPENAI_API_KEY = None
    monkeypatch.setattr(settings, "MAX_RECENT_MESSAGES", 1)
    monkeypatch.setattr(settings, "MEMORY_SUMMARY_EVERY_TURNS", 99)
    monkeypatch.setattr(settings, "MEMORY_REFLECTION_EVERY_TURNS", 99)
    # High enough to reliably include the move-event memory regardless of
    # embedding-rank noise introduced by the player's randomized starting items.
    monkeypatch.setattr(settings, "MEMORY_RELEVANT_ENTRIES", 10)

    narrator_calls = 0

    async def fake_generate_structured(*, messages, model=None, **kwargs):  # noqa: ANN202, ARG001
        return ActionParserOutput(
            action=ActionType.MOVE,
            target="north",
            parameters=ActionParserParameters(),
            stealth=True,
            confidence=0.94,
            parse_status="ok",
            parser_notes=None,
        )

    async def fake_generate_text(*, messages, **kwargs) -> str:
        nonlocal narrator_calls

        narrator_calls += 1
        if narrator_calls == 2:
            assert any(
                "relevant memory:" in str(message["content"]).lower()
                and "grand corridor" in str(message["content"]).lower()
                for message in messages
            )
        return "A haunted reply"

    monkeypatch.setattr(
        action_parser_module.model_client,
        "generate_structured",
        fake_generate_structured,
    )
    monkeypatch.setattr(
        narrator_module.model_client, "generate_text", fake_generate_text
    )

    first_response = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="I go north."),
            owner_user_id=_resolved_internal_user_id(
                TestClient(app), "orchestrator-memory-context"
            ),
        )
    )

    second_response = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(
                message="What do I notice from here?",
                campaign_id=first_response.campaign_id,
            ),
            owner_user_id=_resolved_internal_user_id(
                TestClient(app), "orchestrator-memory-context"
            ),
        )
    )

    assert second_response.campaign_id == first_response.campaign_id


def test_orchestrator_writes_campaign_summary_and_reflection_memory(
    monkeypatch,
) -> None:
    settings.AI_ENABLED = True
    settings.OPENAI_API_KEY = None
    monkeypatch.setattr(settings, "MEMORY_SUMMARY_EVERY_TURNS", 1)
    monkeypatch.setattr(settings, "MEMORY_REFLECTION_EVERY_TURNS", 1)

    async def fake_generate_structured(*, messages, model=None, **kwargs):  # noqa: ANN202, ARG001
        return ActionParserOutput(
            action=ActionType.MOVE,
            target="north",
            parameters=ActionParserParameters(),
            stealth=False,
            confidence=0.91,
            parse_status="ok",
            parser_notes=None,
        )

    async def fake_generate_text(*, messages, **kwargs) -> str:
        prompt = str(messages[0]["content"])

        if "Summarize the campaign" in prompt:
            return "The player moved into the grand corridor and the hall answered in silence."

        if "What important long-term facts should be remembered" in prompt:
            return json.dumps(["The player is in the grand corridor"])

        return "A haunted reply"

    monkeypatch.setattr(
        action_parser_module.model_client,
        "generate_structured",
        fake_generate_structured,
    )
    monkeypatch.setattr(
        narrator_module.model_client, "generate_text", fake_generate_text
    )

    response = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="I go north."),
            owner_user_id=_resolved_internal_user_id(
                TestClient(app), "orchestrator-memory-summary"
            ),
        )
    )

    with session() as db:
        summary = db.get_latest_summary(response.campaign_id)
        assert summary is not None
        assert (
            summary.summary
            == "The player moved into the grand corridor and the hall answered in silence."
        )

        memories = db.list_campaign_memories(response.campaign_id, limit=20)
        assert any(
            memory.kind == "reflection" and "grand corridor" in memory.content.lower()
            for memory in memories
        )
        assert any(
            memory.kind == "event" and "grand corridor" in memory.content.lower() for memory in memories
        )


def test_orchestrator_logs_memory_agent_usage(monkeypatch) -> None:
    settings.AI_ENABLED = True
    settings.OPENAI_API_KEY = None
    monkeypatch.setattr(settings, "MEMORY_SUMMARY_EVERY_TURNS", 1)
    monkeypatch.setattr(settings, "MEMORY_REFLECTION_EVERY_TURNS", 1)

    async def fake_generate_structured(*, messages, model=None, **kwargs):  # noqa: ANN202, ARG001
        return ActionParserOutput(
            action=ActionType.MOVE,
            target="north",
            parameters=ActionParserParameters(),
            stealth=False,
            confidence=0.91,
            parse_status="ok",
            parser_notes=None,
        )

    async def fake_generate_text(*, messages, **kwargs) -> str:
        prompt = str(messages[0]["content"])

        if "Summarize the campaign" in prompt:
            return "The player moved into the grand corridor and the hall answered in silence."

        if "What important long-term facts should be remembered" in prompt:
            return json.dumps(["The player is in the grand corridor"])

        return "A haunted reply"

    monkeypatch.setattr(
        action_parser_module.model_client,
        "generate_structured",
        fake_generate_structured,
    )
    monkeypatch.setattr(
        narrator_module.model_client, "generate_text", fake_generate_text
    )

    response = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="I go north."),
            owner_user_id=_resolved_internal_user_id(
                TestClient(app), "orchestrator-memory-usage"
            ),
        )
    )

    with session() as db:
        rows = db.conn.execute(
            "SELECT agent_name FROM model_requests WHERE campaign_id = ?",
            (response.campaign_id,),
        ).fetchall()
        agent_names = {row["agent_name"] for row in rows}
        assert "MemorySummarizer" in agent_names
        assert "MemoryReflection" in agent_names


def test_orchestrator_uses_policy_models_per_agent(monkeypatch) -> None:
    settings.AI_ENABLED = True
    settings.OPENAI_API_KEY = "test-key"
    monkeypatch.setattr(settings, "MEMORY_SUMMARY_EVERY_TURNS", 1)
    monkeypatch.setattr(settings, "MEMORY_REFLECTION_EVERY_TURNS", 1)

    observed_models: dict[str, str] = {}

    async def fake_generate_structured(*, messages, model=None, **kwargs):  # noqa: ANN202, ARG001
        observed_models["ActionParser"] = str(model)
        return ActionParserOutput(
            action=ActionType.MOVE,
            target="north",
            parameters=ActionParserParameters(),
            stealth=False,
            confidence=0.91,
            parse_status="ok",
            parser_notes=None,
        )

    async def fake_generate_text(*, messages, model=None, **kwargs) -> str:  # noqa: ANN003
        prompt = str(messages[0]["content"])

        if "Summarize the campaign" in prompt:
            observed_models["MemorySummarizer"] = str(model)
            return "The player moved into the grand corridor and the hall answered in silence."

        if "What important long-term facts should be remembered" in prompt:
            observed_models["MemoryReflection"] = str(model)
            return json.dumps(["The player is in the grand corridor"])

        observed_models["Narrator"] = str(model)
        return "A haunted reply"

    monkeypatch.setattr(
        action_parser_module.model_client,
        "generate_structured",
        fake_generate_structured,
    )
    monkeypatch.setattr(
        narrator_module.model_client, "generate_text", fake_generate_text
    )

    asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="I go north."),
            owner_user_id=_resolved_internal_user_id(
                TestClient(app), "orchestrator-policy"
            ),
        )
    )

    assert observed_models["ActionParser"] == ModelPolicy.action_parser_model()
    assert observed_models["Narrator"] == ModelPolicy.narrator_model()
    assert observed_models["MemorySummarizer"] == ModelPolicy.summarizer_model()
    assert observed_models["MemoryReflection"] == ModelPolicy.memory_reflection_model()
