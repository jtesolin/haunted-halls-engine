from fastapi.testclient import TestClient

from app.main import app


def test_chat_echoes_message() -> None:
    client = TestClient(app)

    response = client.post("/api/chat", json={"message": "hello", "player_id": "player-1"})

    assert response.status_code == 200
    data = response.json()

    assert data["reply"] == "AI narrator replies: hello"
    assert data["campaign_id"] == ""
    assert data["turn_id"].startswith("turn_")


def test_chat_requires_real_player_id() -> None:
    client = TestClient(app)

    response = client.post("/api/chat", json={"message": "hello"})

    assert response.status_code == 422


def test_campaign_routes_reject_anonymous_player_id() -> None:
    client = TestClient(app)

    response = client.get("/api/campaigns/anonymous")

    assert response.status_code == 422
