from fastapi.testclient import TestClient

from app.main import app


def test_chat_echoes_message() -> None:
    client = TestClient(app)

    response = client.post("/api/chat", json={"message": "hello"})

    assert response.status_code == 200
    assert response.json() == {"message": "Did you say: hello"}
