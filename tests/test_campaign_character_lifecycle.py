import asyncio

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import settings
from app.db.repositories import STARTER_CHARACTER_NAME
from app.db.session import get_engine, session
from app.orchestration import orchestrator as orchestrator_module
from app.schemas.chat import ChatRequest
from app.api.dependencies import INTERNAL_USER_ID_HEADER_NAME
from tests.test_character_detail import _client, _headers

pytestmark = pytest.mark.usefixtures("fake_runtime_model_provider")


def _create(client: TestClient, headers) -> dict:
    response = client.post("/api/campaign", json={}, headers=headers)
    assert response.status_code == 201
    return response.json()


def _character_rows(campaign_id: str) -> list[str]:
    with get_engine().connect() as conn:
        return [
            row[0]
            for row in conn.execute(
                text("SELECT character_id FROM characters WHERE campaign_id = :c"),
                {"c": campaign_id},
            )
        ]


def test_created_campaign_has_usable_character_end_to_end() -> None:
    client = _client()
    headers = _headers(client, "lifecycle-e2e")
    created = _create(client, headers)
    campaign_id = created["campaign_id"]
    character_id = created["character_id"]
    assert character_id
    assert _character_rows(campaign_id) == [character_id]

    detail = client.get(f"/api/campaign/{campaign_id}", headers=headers).json()
    assert detail["character_id"] == character_id
    listed = client.get("/api/campaigns", headers=headers).json()
    assert [c["character_id"] for c in listed if c["campaign_id"] == campaign_id] == [
        character_id
    ]

    sheet = client.get(f"/api/character/{character_id}", headers=headers)
    assert sheet.status_code == 200
    body = sheet.json()
    assert body["name"] == STARTER_CHARACTER_NAME
    # Starter inventory/abilities are projected from the persisted campaign state.
    assert len(body["abilities"]) >= 2
    assert body["inventory"]

    # Repeated reads never create additional characters.
    for _ in range(2):
        client.get(f"/api/campaign/{campaign_id}", headers=headers)
        client.get(f"/api/character/{character_id}", headers=headers)
    assert _character_rows(campaign_id) == [character_id]


def test_second_campaign_gets_its_own_character_and_other_user_is_denied() -> None:
    client = _client()
    owner = _headers(client, "lifecycle-multi-owner")
    other = _headers(client, "lifecycle-multi-other")
    first = _create(client, owner)
    second = _create(client, owner)
    assert first["character_id"] != second["character_id"]
    assert client.get(f"/api/character/{second['character_id']}", headers=owner).status_code == 200
    assert client.get(f"/api/character/{second['character_id']}", headers=other).status_code == 404


def test_ensure_campaign_character_is_idempotent_on_retry() -> None:
    client = _client()
    created = _create(client, _headers(client, "lifecycle-retry"))
    with session() as db:
        again = db.ensure_campaign_character(created["campaign_id"])
        third = db.ensure_campaign_character(created["campaign_id"])
    assert again == third == created["character_id"]
    assert _character_rows(created["campaign_id"]) == [created["character_id"]]


def test_character_failure_rolls_back_campaign(monkeypatch) -> None:
    client = _client()
    headers = _headers(client, "lifecycle-rollback")

    def boom(self, campaign_id):  # noqa: ANN001, ANN202, ARG001
        raise RuntimeError("character insert failed")

    with monkeypatch.context() as patch:
        patch.setattr("app.db.repositories.Repository.ensure_campaign_character", boom)
        failing = TestClient(client.app, raise_server_exceptions=False)
        response = failing.post("/api/campaign", json={}, headers=headers)
    assert response.status_code == 500
    with get_engine().connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM campaigns")).scalar_one() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM characters")).scalar_one() == 0

    # Retry after the failure succeeds with exactly one campaign and character.
    created = _create(client, headers)
    assert _character_rows(created["campaign_id"]) == [created["character_id"]]


def test_auto_created_chat_campaign_gets_character() -> None:
    client = _client()
    headers = _headers(client, "lifecycle-chat")
    owner = headers[INTERNAL_USER_ID_HEADER_NAME]
    response = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="look around"), owner_user_id=owner
        )
    )
    rows = _character_rows(response.campaign_id)
    assert len(rows) == 1
    detail = client.get(f"/api/campaign/{response.campaign_id}", headers=headers).json()
    assert detail["character_id"] == rows[0]
    asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="look around", campaign_id=response.campaign_id),
            owner_user_id=owner,
        )
    )
    assert _character_rows(response.campaign_id) == rows


def test_backfill_migration_repairs_legacy_campaigns_without_touching_others(tmp_path) -> None:
    original = settings.DATABASE_URL
    settings.DATABASE_URL = f"sqlite:///{tmp_path / 'backfill.db'}"
    try:
        cfg = Config("alembic.ini")
        command.upgrade(cfg, "0004_chat_idempotency")
        with get_engine().begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO internal_users (user_id, identity_provider, provider_issuer, "
                    "provider_subject, email, email_verified, created_at, updated_at, last_login_at) "
                    "VALUES ('u1','google','iss','sub','e@example.com',1,'t','t','t')"
                )
            )
            for cid in ("campaign_legacy", "campaign_has_char"):
                conn.execute(
                    text(
                        "INSERT INTO campaigns (campaign_id, owner_user_id, name, created_at) "
                        "VALUES (:c, 'u1', 'n', '2026-01-01T00:00:00')"
                    ),
                    {"c": cid},
                )
            conn.execute(
                text(
                    "INSERT INTO characters (character_id, campaign_id, name, created_at) "
                    "VALUES ('existing', 'campaign_has_char', 'Keep', '2026-01-01T00:00:00')"
                )
            )
        command.upgrade(cfg, "head")
        with get_engine().connect() as conn:
            rows = {
                row[0]: row[1]
                for row in conn.execute(
                    text("SELECT campaign_id, character_id FROM characters")
                )
            }
        assert rows == {
            "campaign_legacy": "character_campaign_legacy",
            "campaign_has_char": "existing",
        }
    finally:
        settings.DATABASE_URL = original
