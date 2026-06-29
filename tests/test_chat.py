from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app


def test_chat_echoes_message() -> None:
    settings.INTERNAL_API_TOKEN = "test-token"
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
    client = TestClient(app)

    response = client.get(
        "/api/campaigns/anonymous",
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 422
