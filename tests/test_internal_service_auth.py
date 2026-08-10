from app.main import app
from app.core.config import settings
from app.api.dependencies import INTERNAL_USER_ID_HEADER_NAME
from app.schemas.internal_auth import CANONICAL_GOOGLE_ISSUER
from fastapi.testclient import TestClient


def _auth_headers(token: str, user_id: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if user_id is not None:
        headers[INTERNAL_USER_ID_HEADER_NAME] = user_id
    return headers


def _resolve_internal_user_id(
    client: TestClient, provider_subject: str = "internal-auth-user"
) -> str:
    response = client.post(
        "/internal/auth/users/resolve",
        json={
            "identity_provider": "google",
            "provider_issuer": CANONICAL_GOOGLE_ISSUER,
            "provider_subject": provider_subject,
            "email": f"{provider_subject}@example.com",
            "email_verified": True,
            "display_name": "Internal Auth User",
            "avatar_url": "https://example.com/avatar.png",
        },
        headers={
            "Authorization": f"Bearer {settings.INTERNAL_ENGINE_SERVICE_TOKEN or ''}"
        },
    )
    assert response.status_code == 200
    return response.json()["user_id"]


def test_protected_endpoint_rejects_missing_authorization_header() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/chat",
        json={"message": "hello"},
    )

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_protected_endpoint_rejects_wrong_authentication_scheme() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/chat",
        json={"message": "hello"},
        headers={"Authorization": "Basic Zm9vOmJhcg=="},
    )

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_protected_endpoint_rejects_empty_bearer_token() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/chat",
        json={"message": "hello"},
        headers={"Authorization": "Bearer   "},
    )

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_protected_endpoint_rejects_incorrect_token() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/chat",
        json={"message": "hello"},
        headers=_auth_headers("wrong-token"),
    )

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert "wrong-token" not in response.text


def test_protected_endpoint_accepts_the_configured_token() -> None:
    settings.AI_ENABLED = False
    settings.OPENAI_API_KEY = None
    client = TestClient(app)
    user_id = _resolve_internal_user_id(client, "service-auth-accept")

    response = client.post(
        "/api/chat",
        json={"message": "hello"},
        headers=_auth_headers(settings.INTERNAL_ENGINE_SERVICE_TOKEN or "", user_id),
    )

    assert response.status_code == 200


def test_user_scoped_endpoint_rejects_missing_user_context_header() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/chat",
        json={"message": "hello"},
        headers=_auth_headers(settings.INTERNAL_ENGINE_SERVICE_TOKEN or ""),
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Unauthorized"


def test_user_scoped_endpoint_rejects_empty_user_context_header() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/chat",
        json={"message": "hello"},
        headers=_auth_headers(settings.INTERNAL_ENGINE_SERVICE_TOKEN or "", "   "),
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Unauthorized"


def test_user_scoped_endpoint_rejects_malformed_user_context_header() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/chat",
        json={"message": "hello"},
        headers=_auth_headers(
            settings.INTERNAL_ENGINE_SERVICE_TOKEN or "", "not-a-valid-user-id"
        ),
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Unauthorized"


def test_user_scoped_endpoint_rejects_unknown_internal_user_id() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/chat",
        json={"message": "hello"},
        headers=_auth_headers(
            settings.INTERNAL_ENGINE_SERVICE_TOKEN or "",
            "user_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        ),
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Unauthorized"


def test_user_scoped_endpoint_rejects_multiple_user_context_values() -> None:
    client = TestClient(app)
    user_id = _resolve_internal_user_id(client, "service-auth-duplicate-header")

    response = client.post(
        "/api/chat",
        json={"message": "hello"},
        headers=[
            ("Authorization", f"Bearer {settings.INTERNAL_ENGINE_SERVICE_TOKEN or ''}"),
            (INTERNAL_USER_ID_HEADER_NAME, user_id),
            (INTERNAL_USER_ID_HEADER_NAME, "user_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
        ],
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Unauthorized"


def test_route_logic_is_not_invoked_after_failed_service_auth(monkeypatch) -> None:
    from app.orchestration import orchestrator as orchestrator_module

    called = False

    async def fake_handle_chat(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("route logic should not run after auth failure")

    monkeypatch.setattr(
        orchestrator_module.orchestrator, "handle_chat", fake_handle_chat
    )
    client = TestClient(app)

    response = client.post(
        "/api/chat",
        json={"message": "hello"},
        headers={"Authorization": "Token nope"},
    )

    assert response.status_code == 401
    assert called is False


def test_route_logic_is_not_invoked_after_failed_user_context_validation(
    monkeypatch,
) -> None:
    from app.orchestration import orchestrator as orchestrator_module

    called = False

    async def fake_handle_chat(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError(
            "route logic should not run after user context validation failure"
        )

    monkeypatch.setattr(
        orchestrator_module.orchestrator, "handle_chat", fake_handle_chat
    )
    client = TestClient(app)

    response = client.post(
        "/api/chat",
        json={"message": "hello", "internal_user_id": "spoofed"},
        headers=_auth_headers(settings.INTERNAL_ENGINE_SERVICE_TOKEN or ""),
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
        json={"message": "hello"},
        headers=_auth_headers("leaked-secret"),
    )

    assert response.status_code == 401
    assert "leaked-secret" not in response.text


def test_service_only_internal_resolution_allows_missing_user_context_header() -> None:
    client = TestClient(app)

    response = client.post(
        "/internal/auth/users/resolve",
        json={
            "identity_provider": "google",
            "provider_issuer": CANONICAL_GOOGLE_ISSUER,
            "provider_subject": "service-only-resolution",
            "email": "service-only-resolution@example.com",
            "email_verified": True,
            "display_name": "Service Only",
            "avatar_url": "https://example.com/avatar.png",
        },
        headers=_auth_headers(settings.INTERNAL_ENGINE_SERVICE_TOKEN or ""),
    )

    assert response.status_code == 200
    assert response.json()["user_id"].startswith("user_")
