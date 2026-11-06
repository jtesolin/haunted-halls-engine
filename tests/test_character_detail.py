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
    assert response.json() == {"detail": "Character state is unavailable"}
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


def _valid_state() -> dict:
    return build_fresh_campaign_state()


def _state_items_not_object(state: dict) -> None:
    state["items"] = ["secret-marker"]


def _state_item_not_object(state: dict) -> None:
    state["items"]["secret-marker"] = "oops"


def _state_quantity_string(state: dict) -> None:
    next(iter(state["items"].values()))["quantity"] = "secret-marker"


def _state_quantity_zero(state: dict) -> None:
    next(iter(state["items"].values()))["quantity"] = 0


def _state_missing_name(state: dict) -> None:
    del next(iter(state["items"].values()))["name"]


def _state_player_not_object(state: dict) -> None:
    state["player"] = "secret-marker"


def _state_quantity_bool(state: dict) -> None:
    next(iter(state["items"].values()))["quantity"] = True


def _state_missing_description(state: dict) -> None:
    del next(iter(state["items"].values()))["description"]


def _state_progression_null(state: dict) -> None:
    state["player"]["progression"] = None


def _state_progression_not_object(state: dict) -> None:
    state["player"]["progression"] = ["secret-marker"]


def _state_tracks_not_dict(state: dict) -> None:
    state["player"]["progression"] = {"tracks": ["secret-marker"], "unlocked_abilities": []}


def _state_track_out_of_range(state: dict) -> None:
    state["player"]["progression"] = {"tracks": {"investigation": 99}, "unlocked_abilities": []}


def _state_track_string(state: dict) -> None:
    state["player"]["progression"] = {"tracks": {"occult": "secret-marker"}, "unlocked_abilities": []}


def _state_track_bool(state: dict) -> None:
    state["player"]["progression"] = {"tracks": {"occult": True}, "unlocked_abilities": []}


def _state_abilities_duplicate(state: dict) -> None:
    state["player"]["progression"] = {"tracks": {}, "unlocked_abilities": ["keen_eye", "keen_eye"]}


def _state_abilities_blank(state: dict) -> None:
    state["player"]["progression"] = {"tracks": {}, "unlocked_abilities": [" "]}


def _state_abilities_untrimmed(state: dict) -> None:
    state["player"]["progression"] = {"tracks": {}, "unlocked_abilities": [" keen_eye"]}


def _state_generated_abilities_invalid(state: dict) -> None:
    state["player"]["generated_abilities"] = [{"ability_id": "secret-marker"}]


def _state_abilities_not_list(state: dict) -> None:
    state["player"]["progression"] = {"unlocked_abilities": "secret-marker", "tracks": {}}


@pytest.mark.parametrize(
    "corrupt",
    [
        _state_items_not_object,
        _state_item_not_object,
        _state_quantity_string,
        _state_quantity_zero,
        _state_missing_name,
        _state_player_not_object,
        _state_abilities_not_list,
        _state_quantity_bool,
        _state_missing_description,
        _state_progression_null,
        _state_progression_not_object,
        _state_tracks_not_dict,
        _state_track_out_of_range,
        _state_track_string,
        _state_track_bool,
        _state_abilities_duplicate,
        _state_abilities_blank,
        _state_abilities_untrimmed,
        _state_generated_abilities_invalid,
    ],
)
def test_character_detail_rejects_structurally_invalid_state(corrupt) -> None:
    client = _client()
    headers = _headers(client, f"detail-struct-{corrupt.__name__}")
    character_id = f"char-struct-{corrupt.__name__}"
    campaign_id = _campaign_with_character(client, headers, character_id)
    state = _valid_state()
    corrupt(state)
    _set_state(campaign_id, json.dumps(state))

    response = client.get(f"/api/character/{character_id}", headers=headers)

    assert response.status_code == 500
    assert response.json() == {"detail": "Character state is unavailable"}
    assert "secret-marker" not in response.text


@pytest.mark.parametrize("raw", ["null", "[]", "42", "not json secret-marker"])
def test_character_detail_rejects_non_object_state_text(raw: str) -> None:
    client = _client()
    headers = _headers(client, f"detail-raw-{abs(hash(raw))}")
    character_id = f"char-raw-{abs(hash(raw))}"
    campaign_id = _campaign_with_character(client, headers, character_id)
    _set_state(campaign_id, raw)

    response = client.get(f"/api/character/{character_id}", headers=headers)

    assert response.status_code == 500
    assert response.json() == {"detail": "Character state is unavailable"}


@pytest.mark.parametrize("raw", ["", "No campaign state yet."])
def test_character_detail_empty_or_sentinel_state_is_empty(raw: str) -> None:
    client = _client()
    headers = _headers(client, f"detail-sentinel-{len(raw)}")
    character_id = f"char-sentinel-{len(raw)}"
    campaign_id = _campaign_with_character(client, headers, character_id)
    _set_state(campaign_id, raw)

    response = client.get(f"/api/character/{character_id}", headers=headers)

    assert response.status_code == 200
    assert response.json()["inventory"] == []
    assert response.json()["abilities"] == []


def test_character_detail_empty_object_state_is_empty() -> None:
    client = _client()
    headers = _headers(client, "detail-emptyobj")
    campaign_id = _campaign_with_character(client, headers, "char-emptyobj")
    _set_state(campaign_id, "{}")

    response = client.get("/api/character/char-emptyobj", headers=headers)

    assert response.status_code == 200
    assert response.json()["inventory"] == []
    assert response.json()["abilities"] == []


def test_strict_loader_rejects_empty_and_sentinel_text() -> None:
    from app.game.campaign_state import (
        InvalidCampaignStateError,
        load_strict_campaign_state_for_read,
    )

    for raw in ("", "No campaign state yet."):
        with pytest.raises(InvalidCampaignStateError):
            load_strict_campaign_state_for_read(raw)


def test_character_detail_ignores_unknown_track_keys_and_ability_ids() -> None:
    client = _client()
    headers = _headers(client, "detail-unknown-keys")
    campaign_id = _campaign_with_character(client, headers, "char-unknown-keys")
    state = _valid_state()
    state["player"]["progression"] = {
        "tracks": {"investigation": 2, "mystery_track": 999, "x": "bad"},
        "unlocked_abilities": ["keen_eye", "ghost_ability"],
    }
    _set_state(campaign_id, json.dumps(state))

    response = client.get("/api/character/char-unknown-keys", headers=headers)

    assert response.status_code == 200
    abilities = response.json()["abilities"]
    assert [a["ability_id"] for a in abilities] == ["keen_eye"]
    assert abilities[0]["available"] is True
    assert abilities[0]["availability_reason"] is None
