import asyncio
import json
from collections.abc import Iterator

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.agents import action_parser as action_parser_module
from app.agents import narrator as narrator_module
from app.orchestration import orchestrator as orchestrator_module
from app.core.config import settings
from app.db.session import session
from app.main import app
from app.schemas.chat import ChatRequest


@pytest.fixture(autouse=True)
def isolated_database(tmp_path) -> Iterator[None]:
    original_database_url = settings.DATABASE_URL
    settings.DATABASE_URL = f"sqlite:///{tmp_path / 'test_chat.db'}"
    try:
        yield
    finally:
        settings.DATABASE_URL = original_database_url


def test_chat_echoes_message() -> None:
    settings.INTERNAL_API_TOKEN = "test-token"
    settings.AI_ENABLED = False
    settings.OPENAI_API_KEY = None
    client = TestClient(app)

    response = client.post(
        "/api/chat",
        json={"message": "hello", "player_id": "player-1"},
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 200
    data = response.json()

    assert data["reply"] == "AI narrator replies (stub): hello"
    assert data["campaign_id"].startswith("campaign_")
    assert data["turn_id"].startswith("turn_")


def test_chat_requires_real_player_id() -> None:
    settings.INTERNAL_API_TOKEN = "test-token"
    settings.AI_ENABLED = False
    settings.OPENAI_API_KEY = None
    client = TestClient(app)

    response = client.post(
        "/api/chat",
        json={"message": "hello"},
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 422


def test_chat_requires_authorization() -> None:
    client = TestClient(app)

    response = client.post("/api/chat", json={"message": "hello", "player_id": "player-1"})

    assert response.status_code == 401


def test_campaign_routes_reject_anonymous_player_id() -> None:
    settings.INTERNAL_API_TOKEN = "test-token"
    settings.AI_ENABLED = False
    settings.OPENAI_API_KEY = None
    client = TestClient(app)

    response = client.get(
        "/api/campaigns/anonymous",
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 422


def test_create_campaign_returns_hydrated_campaign() -> None:
    settings.INTERNAL_API_TOKEN = "test-token"
    settings.AI_ENABLED = False
    settings.OPENAI_API_KEY = None
    client = TestClient(app)

    response = client.post(
        "/api/campaign",
        json={"player_id": "player-3"},
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 201
    data = response.json()

    assert data["campaign_id"].startswith("campaign_")
    assert data["name"] == "The Bell Beneath the Hall"
    assert data["player_id"] == "player-3"
    assert data["truncated"] is False
    assert len(data["messages"]) == 1
    assert data["messages"][0]["role"] == "assistant"
    assert "What do you do first?" in data["messages"][0]["content"]


def test_create_campaign_uses_narrator_for_opening_and_title(monkeypatch) -> None:
    settings.INTERNAL_API_TOKEN = "test-token"
    settings.AI_ENABLED = True
    settings.OPENAI_API_KEY = None
    prompts: list[str] = []

    async def fake_generate_text(*, messages, **kwargs) -> str:
        prompts.append(messages[-1]["content"])
        if "provide only a short haunted campaign title" in messages[-1]["content"].lower():
            return "Whispers in the West Wing"
        return "The candles hiss awake in the corridor. Where do you step first?"

    monkeypatch.setattr(narrator_module.model_client, "generate_text", fake_generate_text)
    client = TestClient(app)

    response = client.post(
        "/api/campaign",
        json={"player_id": "player-4"},
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 201
    data = response.json()

    assert len(prompts) == 2
    assert "Start a new haunted halls campaign" in prompts[0]
    assert data["name"] == "Whispers in the West Wing"
    assert data["messages"][0]["content"] == "The candles hiss awake in the corridor. Where do you step first?"


def test_orchestrator_uses_narrator_agent_and_persists_turn(monkeypatch) -> None:
    settings.AI_ENABLED = True
    settings.OPENAI_API_KEY = None

    saw_tool_context = False

    async def fake_generate_text(*, messages, **kwargs) -> str:
        nonlocal saw_tool_context
        assert messages[0]["role"] == "developer"

        if "Convert player text into strict JSON" in str(messages[0]["content"]):
            return json.dumps(
                {
                    "action": "move",
                    "target": "roof",
                    "parameters": {},
                    "stealth": True,
                    "confidence": 0.94,
                    "parse_status": "ok",
                    "parser_notes": None,
                }
            )

        assert messages[1]["role"] == "user"
        assert "Campaign state:" in messages[1]["content"]
        assert any("Tool execution result:" in str(message["content"]) for message in messages)
        saw_tool_context = True
        assert messages[-1]["role"] == "user"
        assert messages[-1]["content"] == "hello"
        return "A haunted reply"

    monkeypatch.setattr(action_parser_module.model_client, "generate_text", fake_generate_text)
    monkeypatch.setattr(narrator_module.model_client, "generate_text", fake_generate_text)

    response = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="hello", player_id="player-2")
        )
    )

    assert response.reply == "A haunted reply"
    assert saw_tool_context is True

    with session() as db:
        campaign = db.get_campaign(response.campaign_id)
        assert campaign is not None
        assert campaign.state is not None
        campaign_state = json.loads(campaign.state)
        assert campaign_state["player"]["location"] == "roof"

        _, turns, _ = db.get_campaign_with_turns(response.campaign_id, limit=10)
        assert len(turns) >= 2
        assert turns[-2].player_id == "player-2"
        assert turns[-2].role == "user"
        assert turns[-2].content == "hello"
        assert turns[-1].player_id == "player-2"
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
        assert json.loads(events[-1].payload_json or "{}") == {"reply": "A haunted reply"}


def test_orchestrator_records_parse_failure_for_invalid_action(monkeypatch) -> None:
    settings.AI_ENABLED = True
    settings.OPENAI_API_KEY = None

    async def fake_generate_text(*, messages, **kwargs) -> str:
        if "Convert player text into strict JSON" in str(messages[0]["content"]):
            return json.dumps(
                {
                    "action": "",
                    "target": None,
                    "parameters": {},
                    "stealth": False,
                    "confidence": 0.2,
                    "parse_status": "invalid",
                    "parser_notes": "Could not determine action",
                }
            )
        return "I cannot decode your intent, but the hall waits."

    monkeypatch.setattr(action_parser_module.model_client, "generate_text", fake_generate_text)
    monkeypatch.setattr(narrator_module.model_client, "generate_text", fake_generate_text)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            orchestrator_module.orchestrator.handle_chat(
                ChatRequest(message="???", player_id="player-5")
            )
        )

    assert exc_info.value.status_code == 422
    assert "Unprocessable action" in str(exc_info.value.detail)

    with session() as db:
        campaign_count = db.count_player_campaigns("player-5")
        assert campaign_count == 0


def test_orchestrator_returns_502_when_action_parser_provider_fails(monkeypatch) -> None:
    settings.AI_ENABLED = True
    settings.OPENAI_API_KEY = None

    async def broken_generate_text(*, messages, **kwargs):  # noqa: ANN202
        if "Convert player text into strict JSON" in str(messages[0]["content"]):
            raise RuntimeError("parser upstream failure")
        return "unused"

    monkeypatch.setattr(action_parser_module.model_client, "generate_text", broken_generate_text)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            orchestrator_module.orchestrator.handle_chat(
                ChatRequest(message="move to roof", player_id="player-6")
            )
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "Action parser service failed."


def test_ai_disabled_still_runs_parser_and_tools() -> None:
    settings.AI_ENABLED = False
    settings.OPENAI_API_KEY = None

    response = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="I quietly climb onto the roof.", player_id="player-7")
        )
    )

    assert response.reply == "AI narrator replies (stub): I quietly climb onto the roof."

    with session() as db:
        campaign = db.get_campaign(response.campaign_id)
        assert campaign is not None
        assert campaign.state is not None
        campaign_state = json.loads(campaign.state)
        assert campaign_state["player"]["location"] == "roof"

        events = db.list_campaign_events(response.campaign_id)
        assert [event.type for event in events[-5:]] == [
            "player_message_received",
            "action_parsed",
            "tool_executed",
            "game_state_updated",
            "narrator_response_created",
        ]
