import asyncio

from fastapi.testclient import TestClient

from app.ai import orchestrator as orchestrator_module
from app.core.config import settings
from app.db.session import session
from app.main import app
from app.schemas.chat import ChatRequest


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
    assert data["campaign_id"].startswith("auto_")
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


def test_orchestrator_uses_narrator_agent_and_persists_turn(monkeypatch) -> None:
    settings.AI_ENABLED = True
    settings.OPENAI_API_KEY = None

    async def fake_generate_text(prompt: str, **kwargs) -> str:
        assert "Recent conversation" in prompt
        assert "Player says: hello" in prompt
        return "A haunted reply"

    monkeypatch.setattr(orchestrator_module.model_client, "generate_text", fake_generate_text)

    response = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="hello", player_id="player-2")
        )
    )

    assert response.reply == "A haunted reply"

    with session() as db:
        _, turns, _ = db.get_campaign_with_turns(response.campaign_id, limit=10)
        assert turns[-1].player_message == "hello"
        assert turns[-1].ai_reply == "A haunted reply"
