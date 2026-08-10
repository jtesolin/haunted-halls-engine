import sqlite3

from fastapi.testclient import TestClient

from app.api.dependencies import INTERNAL_USER_ID_HEADER_NAME
from app.core.config import settings
from app.db.session import init_db
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


def test_campaign_creation_persists_owner_from_authenticated_user_context() -> None:
    settings.INTERNAL_ENGINE_SERVICE_TOKEN = "test-token"
    settings.AI_ENABLED = False
    settings.OPENAI_API_KEY = None
    client = TestClient(app)
    headers = _user_scoped_headers(client, "campaign-owner")

    response = client.post(
        "/api/campaign",
        json={"player_id": "player-owner-1"},
        headers=headers,
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["player_id"] == "player-owner-1"

    with sqlite3.connect(settings.DATABASE_URL.removeprefix("sqlite:///")) as conn:
        row = conn.execute(
            "SELECT owner_user_id, player_id FROM campaigns WHERE campaign_id = ?",
            (payload["campaign_id"],),
        ).fetchone()

    assert row is not None
    assert row[0] == headers[INTERNAL_USER_ID_HEADER_NAME]
    assert row[1] == "player-owner-1"


def test_campaign_creation_ignores_owner_provided_in_request_body() -> None:
    settings.INTERNAL_ENGINE_SERVICE_TOKEN = "test-token"
    settings.AI_ENABLED = False
    settings.OPENAI_API_KEY = None
    client = TestClient(app)
    headers = _user_scoped_headers(client, "campaign-owner-body")

    response = client.post(
        "/api/campaign",
        json={"player_id": "player-owner-2", "owner_user_id": "user_should_not_be_used"},
        headers=headers,
    )

    assert response.status_code == 201
    payload = response.json()

    with sqlite3.connect(settings.DATABASE_URL.removeprefix("sqlite:///")) as conn:
        row = conn.execute(
            "SELECT owner_user_id FROM campaigns WHERE campaign_id = ?",
            (payload["campaign_id"],),
        ).fetchone()

    assert row is not None
    assert row[0] == headers[INTERNAL_USER_ID_HEADER_NAME]


def test_init_db_migrates_existing_campaign_rows_with_nullable_owner(tmp_path) -> None:
    db_path = tmp_path / "legacy-campaigns.db"
    db_url = f"sqlite:///{db_path}"
    original_database_url = settings.DATABASE_URL
    settings.DATABASE_URL = db_url
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                CREATE TABLE campaigns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    campaign_id TEXT NOT NULL UNIQUE,
                    player_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT,
                    state TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE internal_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL UNIQUE,
                    identity_provider TEXT NOT NULL,
                    provider_issuer TEXT NOT NULL,
                    provider_subject TEXT NOT NULL,
                    email TEXT NOT NULL,
                    email_verified INTEGER NOT NULL,
                    display_name TEXT,
                    avatar_url TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_login_at TEXT NOT NULL,
                    UNIQUE(provider_issuer, provider_subject)
                )
                """
            )
            conn.execute(
                "INSERT INTO campaigns (campaign_id, player_id, name, description, state, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("campaign_legacy", "player-legacy", "Legacy Campaign", None, None, "2024-01-01T00:00:00"),
            )
            conn.commit()
            init_db(conn)
            conn.commit()

            columns = {row[1] for row in conn.execute("PRAGMA table_info(campaigns)")}
            assert "owner_user_id" in columns

            row = conn.execute(
                "SELECT owner_user_id FROM campaigns WHERE campaign_id = ?",
                ("campaign_legacy",),
            ).fetchone()
            assert row is not None
            assert row[0] is None
    finally:
        settings.DATABASE_URL = original_database_url
