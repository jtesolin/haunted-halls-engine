import asyncio

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import settings
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
    assert isinstance(body["name"], str) and body["name"].strip()
    with get_engine().connect() as conn:
        stored = conn.execute(
            text("SELECT name FROM characters WHERE character_id = :c"),
            {"c": character_id},
        ).scalar_one()
    assert body["name"] == stored
    # Starter inventory/abilities are projected from the persisted campaign state.
    assert len(body["abilities"]) >= 2
    assert body["inventory"]

    # Repeated reads never create additional characters.
    for _ in range(2):
        client.get(f"/api/campaign/{campaign_id}", headers=headers)
        client.get(f"/api/character/{character_id}", headers=headers)
    assert _character_rows(campaign_id) == [character_id]


def test_character_name_comes_from_generator_and_is_persisted(monkeypatch) -> None:
    names = iter(["Ada Lovelace", "Grace Hopper"])
    monkeypatch.setattr(
        orchestrator_module, "generate_character_name", lambda: next(names)
    )
    client = _client()
    headers = _headers(client, "lifecycle-name")
    first = _create(client, headers)
    second = _create(client, headers)
    for created, expected in ((first, "Ada Lovelace"), (second, "Grace Hopper")):
        sheet = client.get(f"/api/character/{created['character_id']}", headers=headers)
        assert sheet.json()["name"] == expected


def test_generate_character_name_returns_nonempty_names() -> None:
    from app.game.character_names import generate_character_name

    names = {generate_character_name() for _ in range(20)}
    assert all(isinstance(name, str) and name.strip() for name in names)
    assert len(names) > 1


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
        again = db.ensure_campaign_character(created["campaign_id"], "Retry One")
        third = db.ensure_campaign_character(created["campaign_id"], "Retry Two")
    assert again == third == created["character_id"]
    # A retry never renames or replaces the existing character.
    with get_engine().connect() as conn:
        name = conn.execute(
            text("SELECT name FROM characters WHERE character_id = :c"),
            {"c": created["character_id"]},
        ).scalar_one()
    assert name not in {"Retry One", "Retry Two"}
    assert _character_rows(created["campaign_id"]) == [created["character_id"]]


def test_character_failure_rolls_back_campaign(monkeypatch) -> None:
    client = _client()
    headers = _headers(client, "lifecycle-rollback")

    def boom(self, campaign_id, name):  # noqa: ANN001, ANN202, ARG001
        raise RuntimeError("character insert failed")

    with monkeypatch.context() as patch:
        patch.setattr("app.db.repositories.Repository.ensure_campaign_character", boom)
        failing = TestClient(client.app, raise_server_exceptions=False)
        response = failing.post("/api/campaign", json={}, headers=headers)
    assert response.status_code == 500
    with get_engine().connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM campaigns")).scalar_one() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM characters")).scalar_one() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM turns")).scalar_one() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM game_events")).scalar_one() == 0

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
        with get_engine().connect() as conn:
            names = {
                row[0]: row[1]
                for row in conn.execute(
                    text("SELECT campaign_id, name FROM characters")
                )
            }
        assert names["campaign_has_char"] == "Keep"
        assert names["campaign_legacy"].strip()
        assert names["campaign_legacy"] != "Keep"
    finally:
        settings.DATABASE_URL = original


def _seed_user_and_campaigns(conn, campaign_ids) -> None:
    conn.execute(
        text(
            "INSERT INTO internal_users (user_id, identity_provider, provider_issuer, "
            "provider_subject, email, email_verified, created_at, updated_at, last_login_at) "
            "VALUES ('u1','google','iss','sub','e@example.com',1,'t','t','t')"
        )
    )
    for cid in campaign_ids:
        conn.execute(
            text(
                "INSERT INTO campaigns (campaign_id, owner_user_id, name, created_at) "
                "VALUES (:c, 'u1', 'n', '2026-01-01T00:00:00')"
            ),
            {"c": cid},
        )


def _snapshot() -> list[tuple[str, str, str]]:
    with get_engine().connect() as conn:
        return [
            (row[0], row[1], row[2])
            for row in conn.execute(
                text(
                    "SELECT character_id, campaign_id, name FROM characters "
                    "ORDER BY character_id"
                )
            )
        ]


def _alembic_revision() -> str:
    with get_engine().connect() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def test_backfill_rerun_is_idempotent(tmp_path) -> None:
    original = settings.DATABASE_URL
    settings.DATABASE_URL = f"sqlite:///{tmp_path / 'backfill-rerun.db'}"
    try:
        cfg = Config("alembic.ini")
        command.upgrade(cfg, "0004_chat_idempotency")
        with get_engine().begin() as conn:
            _seed_user_and_campaigns(conn, ["campaign_a", "campaign_b"])
        command.upgrade(cfg, "head")
        first = _snapshot()
        assert all(row[2].strip() for row in first)
        assert [row[0] for row in first] == [
            "character_campaign_a",
            "character_campaign_b",
        ]
        # Re-run the real migration against the already-backfilled data.
        command.stamp(cfg, "0004_chat_idempotency")
        command.upgrade(cfg, "head")
        assert _snapshot() == first
    finally:
        settings.DATABASE_URL = original


def test_backfill_character_id_collision_fails_safely_and_clearly(tmp_path) -> None:
    original = settings.DATABASE_URL
    settings.DATABASE_URL = f"sqlite:///{tmp_path / 'backfill-collision.db'}"
    try:
        cfg = Config("alembic.ini")
        command.upgrade(cfg, "0004_chat_idempotency")
        with get_engine().begin() as conn:
            _seed_user_and_campaigns(
                conn, ["campaign_a", "campaign_b", "campaign_c"]
            )
            # campaign_a has a character; a foreign row squats campaign_b's
            # deterministic id, so campaign_b cannot receive its starter row.
            conn.execute(
                text(
                    "INSERT INTO characters (character_id, campaign_id, name, created_at) "
                    "VALUES ('existing_a', 'campaign_a', 'Keep', '2026-01-01T00:00:00'), "
                    "('character_campaign_b', 'campaign_a', 'Squatter', '2026-01-01T00:00:00')"
                )
            )
        before = _snapshot()

        with pytest.raises(RuntimeError, match="campaign_b") as excinfo:
            command.upgrade(cfg, "head")
        assert "campaign_c" not in str(excinfo.value)
        # Fails before changing anything: no partial backfill (campaign_c is
        # untouched) and the revision is not advanced.
        assert _snapshot() == before
        assert _alembic_revision() == "0004_chat_idempotency"

        # After the operator resolves the conflict the migration succeeds and
        # every legacy campaign ends with a character.
        with get_engine().begin() as conn:
            conn.execute(
                text("DELETE FROM characters WHERE character_id = 'character_campaign_b'")
            )
        command.upgrade(cfg, "head")
        assert {row[1] for row in _snapshot()} == {
            "campaign_a",
            "campaign_b",
            "campaign_c",
        }
        assert _alembic_revision() == "0005_backfill_characters"
    finally:
        settings.DATABASE_URL = original


def test_character_id_collision_fails_creation_without_partial_state(monkeypatch) -> None:
    client = _client()
    headers = _headers(client, "lifecycle-collision")
    existing = _create(client, headers)
    with get_engine().connect() as conn:
        campaigns_before = conn.execute(text("SELECT COUNT(*) FROM campaigns")).scalar_one()
        characters_before = conn.execute(text("SELECT COUNT(*) FROM characters")).scalar_one()

    # Force the new campaign's deterministic id onto an already-used character
    # id: the swallowed IntegrityError leaves no row, reaching the RuntimeError.
    monkeypatch.setattr(
        "app.db.repositories.starter_character_id",
        lambda _campaign_id: existing["character_id"],
    )
    failing = TestClient(client.app, raise_server_exceptions=False)
    response = failing.post("/api/campaign", json={}, headers=headers)
    assert response.status_code == 500

    with get_engine().connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM campaigns")).scalar_one() == campaigns_before
        assert conn.execute(text("SELECT COUNT(*) FROM characters")).scalar_one() == characters_before
        for table in ("turns", "game_events"):
            orphaned = conn.execute(
                text(
                    f"SELECT COUNT(*) FROM {table} WHERE campaign_id NOT IN "
                    "(SELECT campaign_id FROM campaigns)"
                )
            ).scalar_one()
            assert orphaned == 0
