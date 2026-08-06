from fastapi.testclient import TestClient
from starlette.requests import Request

from app.api.dependencies import (
    INTERNAL_USER_ID_HEADER_NAME,
    InternalServiceContext,
    require_authenticated_user_context,
)
from app.core.config import settings
from app.main import app
from app.schemas.internal_auth import CANONICAL_GOOGLE_ISSUER


def _resolve_internal_user_id(client: TestClient, provider_subject: str = "context-test-user") -> str:
    response = client.post(
        "/internal/auth/users/resolve",
        json={
            "identity_provider": "google",
            "provider_issuer": CANONICAL_GOOGLE_ISSUER,
            "provider_subject": provider_subject,
            "email": f"{provider_subject}@example.com",
            "email_verified": True,
            "display_name": "Context Test",
            "avatar_url": "https://example.com/avatar.png",
        },
        headers={"Authorization": f"Bearer {settings.INTERNAL_ENGINE_SERVICE_TOKEN or ''}"},
    )
    assert response.status_code == 200
    return response.json()["user_id"]


def test_authenticated_user_context_contains_expected_internal_user_id() -> None:
    client = TestClient(app)
    user_id = _resolve_internal_user_id(client, "typed-context-user")

    request = Request(
        {
            "type": "http",
            "headers": [
                (
                    INTERNAL_USER_ID_HEADER_NAME.lower().encode("ascii"),
                    user_id.encode("ascii"),
                )
            ],
        }
    )

    context = require_authenticated_user_context(
        request=request,
        service_context=InternalServiceContext(caller_name="test-service"),
    )

    assert context.internal_user_id == user_id
    assert context.service_caller_name == "test-service"
