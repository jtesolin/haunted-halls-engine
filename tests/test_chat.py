from fastapi.testclient import TestClient

from app.main import app


def test_chat_echoes_message() -> None:
    client = TestClient(app)

    response = client.post("/api/chat", json={"message": "hello"})

    assert response.status_code == 200
    assert response.json() == {
        "reply": "AI narrator replies: hello",
        "campaign_id": "",
        "turn_id": "turn_0001",
    }
