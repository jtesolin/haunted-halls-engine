from app.main import app
from app.core.config import settings
from fastapi.testclient import TestClient


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_protected_endpoint_rejects_missing_authorization_header() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/chat",
        json={"message": "hello", "player_id": "player-1"},
    )

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_protected_endpoint_rejects_wrong_authentication_scheme() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/chat",
        json={"message": "hello", "player_id": "player-1"},
        headers={"Authorization": "Basic Zm9vOmJhcg=="},
    )

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_protected_endpoint_rejects_empty_bearer_token() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/chat",
        json={"message": "hello", "player_id": "player-1"},
        headers={"Authorization": "Bearer   "},
    )

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_protected_endpoint_rejects_incorrect_token() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/chat",
        json={"message": "hello", "player_id": "player-1"},
        headers=_auth_headers("wrong-token"),
    )

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert "wrong-token" not in response.text


def test_protected_endpoint_accepts_the_configured_token() -> None:
    settings.AI_ENABLED = False
    settings.OPENAI_API_KEY = None
    client = TestClient(app)

    response = client.post(
        "/api/chat",
        json={"message": "hello", "player_id": "player-1"},
        headers=_auth_headers(settings.INTERNAL_ENGINE_SERVICE_TOKEN or ""),
    )

    assert response.status_code == 200


def test_route_logic_is_not_invoked_after_failed_service_auth(monkeypatch) -> None:
    from app.orchestration import orchestrator as orchestrator_module

    called = False

    async def fake_handle_chat(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("route logic should not run after auth failure")

    monkeypatch.setattr(orchestrator_module.orchestrator, "handle_chat", fake_handle_chat)
    client = TestClient(app)

    response = client.post(
        "/api/chat",
        json={"message": "hello", "player_id": "player-1"},
        headers={"Authorization": "Token nope"},
    )

    assert response.status_code == 401
    assert called is False


def test_public_health_endpoint_remains_accessible_without_the_token() -> None:
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_auth_failures_do_not_echo_supplied_credentials() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/chat",
        json={"message": "hello", "player_id": "player-1"},
        headers=_auth_headers("leaked-secret"),
    )

    assert response.status_code == 401
    assert "leaked-secret" not in response.text