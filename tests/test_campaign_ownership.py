from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.dependencies import INTERNAL_USER_ID_HEADER_NAME
from app.core.config import settings
from app.db.session import get_engine
from app.main import app
from app.schemas.internal_auth import CANONICAL_GOOGLE_ISSUER


def _user_scoped_headers(client: TestClient, provider_subject: str) -> dict[str, str]:
    resolve_response = client.post(
        "/internal/auth/users/resolve",
        json={
            "identity_provider": "google",
            "provider_issuer": CANONICAL_GOOGLE_ISSUER,
            "provider_subject": provider_subject,
            "email": f"{provider_subject}@example.com",
            "email_verified": True,
            "display_name": "Owner Test",
            "avatar_url": "https://example.com/avatar.png",
        },
        headers={"Authorization": "Bearer test-token"},
    )
    assert resolve_response.status_code == 200
    user_id = resolve_response.json()["user_id"]
    return {
        "Authorization": "Bearer test-token",
        INTERNAL_USER_ID_HEADER_NAME: user_id,
    }


def _sqlite_database_path() -> str:
    database_url = settings.DATABASE_URL
    assert database_url is not None
    return database_url.removeprefix("sqlite:///")


def test_campaign_creation_persists_owner_from_authenticated_user_context() -> None:
    settings.INTERNAL_ENGINE_SERVICE_TOKEN = "test-token"
    settings.AI_ENABLED = False
    settings.OPENAI_API_KEY = None
    client = TestClient(app)
    headers = _user_scoped_headers(client, "campaign-owner")

    response = client.post(
        "/api/campaign",
        json={},
        headers=headers,
    )

    assert response.status_code == 201
    payload = response.json()

    with get_engine().connect() as conn:
        row = conn.execute(
            text("SELECT owner_user_id FROM campaigns WHERE campaign_id = :campaign_id"),
            {"campaign_id": payload["campaign_id"]},
        ).fetchone()

    assert row is not None
    assert row[0] == headers[INTERNAL_USER_ID_HEADER_NAME]


def test_campaign_creation_uses_current_schema_columns() -> None:
    settings.INTERNAL_ENGINE_SERVICE_TOKEN = "test-token"
    settings.AI_ENABLED = False
    settings.OPENAI_API_KEY = None
    client = TestClient(app)
    headers = _user_scoped_headers(client, "campaign-owner-body")

    response = client.post(
        "/api/campaign",
        json={},
        headers=headers,
    )

    assert response.status_code == 201
    payload = response.json()

    with get_engine().connect() as conn:
        row = conn.execute(
            text("SELECT owner_user_id FROM campaigns WHERE campaign_id = :campaign_id"),
            {"campaign_id": payload["campaign_id"]},
        ).fetchone()

    assert row is not None
    assert row[0] == headers[INTERNAL_USER_ID_HEADER_NAME]
