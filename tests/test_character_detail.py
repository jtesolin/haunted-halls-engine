import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.dependencies import INTERNAL_USER_ID_HEADER_NAME
from app.core.config import settings
from app.db.session import get_engine
from app.game.campaign_state import build_fresh_campaign_state
from app.main import app
from app.schemas.internal_auth import CANONICAL_GOOGLE_ISSUER

pytestmark = pytest.mark.usefixtures("fake_runtime_model_provider")


def _headers(client: TestClient, subject: str) -> dict[str, str]:
    response = client.post(
        "/internal/auth/users/resolve",
        json={
            "identity_provider": "google",
            "provider_issuer": CANONICAL_GOOGLE_ISSUER,
            "provider_subject": subject,
            "email": f"{subject}@example.com",
            "email_verified": True,
            "display_name": "Detail Test",
            "avatar_url": None,
        },
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 200
    return {
        "Authorization": "Bearer test-token",
        INTERNAL_USER_ID_HEADER_NAME: response.json()["user_id"],
    }


def _client() -> TestClient:
    settings.INTERNAL_ENGINE_SERVICE_TOKEN = "test-token"
    return TestClient(app)


def _insert_character(campaign_id: str, character_id: str, name: str) -> None:
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO characters (character_id, campaign_id, name, created_at) "
                "VALUES (:cid, :camp, :name, '2026-01-01T00:00:00')"
            ),
            {"cid": character_id, "camp": campaign_id, "name": name},
        )


def _set_state(campaign_id: str, state: str | None) -> None:
    with get_engine().begin() as conn:
        conn.execute(
            text("UPDATE campaigns SET state = :s WHERE campaign_id = :c"),
            {"s": state, "c": campaign_id},
        )


def _campaign_with_character(client: TestClient, headers, character_id: str) -> str:
    response = client.post("/api/campaign", json={}, headers=headers)
    assert response.status_code == 201
    campaign_id = response.json()["campaign_id"]
    _insert_character(campaign_id, character_id, "Investigator")
    return campaign_id


def test_character_detail_returns_persisted_inventory_and_abilities() -> None:
    client = _client()
    headers = _headers(client, "detail-populated")
    campaign_id = _campaign_with_character(client, headers, "char-populated")

    with get_engine().connect() as conn:
        persisted = json.loads(
            conn.execute(
                text("SELECT state FROM campaigns WHERE campaign_id = :c"),
                {"c": campaign_id},
            ).scalar_one()
        )
    inventory_ids = persisted["player"]["inventory"]
    owned_ids = persisted["player"]["progression"]["unlocked_abilities"]
    assert inventory_ids and owned_ids

    response = client.get("/api/character/char-populated", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"character_id", "name", "inventory", "abilities"}
    assert body["character_id"] == "char-populated"
    assert body["name"] == "Investigator"
    assert [item["id"] for item in body["inventory"]] == inventory_ids
    for item in body["inventory"]:
        assert set(item) == {"id", "name", "description", "quantity"}
        assert item["quantity"] >= 1
    assert [a["ability_id"] for a in body["abilities"]] == owned_ids
    for ability in body["abilities"]:
        assert set(ability) == {
            "ability_id",
            "display_name",
            "description",
            "available",
            "availability_reason",
        }


def test_character_detail_serializes_empty_collections() -> None:
    client = _client()
    headers = _headers(client, "detail-empty")
    campaign_id = _campaign_with_character(client, headers, "char-empty")
    state = build_fresh_campaign_state()
    for item in state["items"].values():
        if item["location"] == "player:current":
            item["location"] = "none"
    state["player"]["inventory"] = []
    _set_state(campaign_id, json.dumps(state))

    response = client.get("/api/character/char-empty", headers=headers)

    assert response.status_code == 200
    assert response.json()["inventory"] == []
    assert response.json()["abilities"] == []


def test_character_detail_without_persisted_state_is_empty() -> None:
    client = _client()
    headers = _headers(client, "detail-nostate")
    campaign_id = _campaign_with_character(client, headers, "char-nostate")
    _set_state(campaign_id, None)

    response = client.get("/api/character/char-nostate", headers=headers)

    assert response.status_code == 200
    assert response.json()["inventory"] == []
    assert response.json()["abilities"] == []


def test_character_detail_is_owner_scoped() -> None:
    client = _client()
    owner = _headers(client, "detail-owner")
    other = _headers(client, "detail-other")
    _campaign_with_character(client, owner, "char-private")

    assert client.get("/api/character/char-private", headers=other).status_code == 404
    assert client.get("/api/character/char-private", headers=owner).status_code == 200


def test_character_detail_unknown_and_unauthenticated() -> None:
    client = _client()
    headers = _headers(client, "detail-unknown")

    assert client.get("/api/character/missing", headers=headers).status_code == 404
    assert client.get("/api/character/missing").status_code in {401, 403}


def test_character_detail_malformed_state_does_not_leak_payload() -> None:
    client = _client()
    headers = _headers(client, "detail-malformed")
    campaign_id = _campaign_with_character(client, headers, "char-malformed")
    _set_state(campaign_id, "[not-an-object secret-marker")

    response = client.get("/api/character/char-malformed", headers=headers)

    assert response.status_code == 500
    assert "secret-marker" not in response.text


def test_characters_list_remains_lightweight() -> None:
    client = _client()
    headers = _headers(client, "detail-list")
    _campaign_with_character(client, headers, "char-list")

    response = client.get("/api/characters", headers=headers)

    assert response.status_code == 200
    assert response.json() == {
        "characters": [{"character_id": "char-list", "name": "Investigator"}]
    }
