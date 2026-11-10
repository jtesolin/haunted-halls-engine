import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db.session import get_engine
from tests.test_character_detail import _client, _headers, _insert_character

pytestmark = pytest.mark.usefixtures("fake_runtime_model_provider")


def _create_campaign(client: TestClient, headers) -> str:
    response = client.post("/api/campaign", json={}, headers=headers)
    assert response.status_code == 201
    campaign_id = response.json()["campaign_id"]
    # These tests model legacy/seeded association states, so drop the starter
    # character the application now creates (see test_campaign_character_lifecycle).
    with get_engine().begin() as conn:
        conn.execute(
            text("DELETE FROM characters WHERE campaign_id = :c"), {"c": campaign_id}
        )
    return campaign_id


def _list_entry(client: TestClient, headers, campaign_id: str) -> dict:
    response = client.get("/api/campaigns", headers=headers)
    assert response.status_code == 200
    return next(c for c in response.json() if c["campaign_id"] == campaign_id)


def test_campaign_without_character_exposes_null() -> None:
    client = _client()
    headers = _headers(client, "assoc-none")
    campaign_id = _create_campaign(client, headers)

    detail = client.get(f"/api/campaign/{campaign_id}", headers=headers).json()
    assert detail["character_id"] is None
    assert _list_entry(client, headers, campaign_id)["character_id"] is None


def test_campaign_detail_and_list_expose_persisted_character_id() -> None:
    client = _client()
    headers = _headers(client, "assoc-present")
    campaign_id = _create_campaign(client, headers)
    other_campaign_id = _create_campaign(client, headers)
    _insert_character(campaign_id, "char-assoc-1", "Investigator")

    detail = client.get(f"/api/campaign/{campaign_id}", headers=headers).json()
    assert detail["character_id"] == "char-assoc-1"
    assert _list_entry(client, headers, campaign_id)["character_id"] == "char-assoc-1"
    # Unrelated campaign is unaffected.
    assert _list_entry(client, headers, other_campaign_id)["character_id"] is None

    # Relationship is consistent with the character endpoint.
    character = client.get("/api/character/char-assoc-1", headers=headers)
    assert character.status_code == 200
    assert character.json()["character_id"] == detail["character_id"]


def test_multiple_characters_pick_earliest_created_deterministically() -> None:
    client = _client()
    headers = _headers(client, "assoc-multi")
    campaign_id = _create_campaign(client, headers)
    with get_engine().begin() as conn:
        for cid, created in (("char-late", "2026-02-01T00:00:00"), ("char-early", "2026-01-01T00:00:00")):
            conn.execute(
                text(
                    "INSERT INTO characters (character_id, campaign_id, name, created_at) "
                    "VALUES (:cid, :camp, 'N', :created)"
                ),
                {"cid": cid, "camp": campaign_id, "created": created},
            )

    detail = client.get(f"/api/campaign/{campaign_id}", headers=headers).json()
    assert detail["character_id"] == "char-early"
    assert _list_entry(client, headers, campaign_id)["character_id"] == "char-early"


def test_equal_created_at_uses_lower_row_id_as_tie_breaker() -> None:
    client = _client()
    headers = _headers(client, "assoc-tie")
    campaign_id = _create_campaign(client, headers)
    # Inserted first => lower row id, but a lexicographically larger character_id,
    # so passing cannot be explained by ordering on character_id.
    with get_engine().begin() as conn:
        for cid in ("char-z-lower-id", "char-a-higher-id"):
            conn.execute(
                text(
                    "INSERT INTO characters (character_id, campaign_id, name, created_at) "
                    "VALUES (:cid, :camp, 'N', '2026-01-01T00:00:00')"
                ),
                {"cid": cid, "camp": campaign_id},
            )
        # A later row must not win either.
        conn.execute(
            text(
                "INSERT INTO characters (character_id, campaign_id, name, created_at) "
                "VALUES ('char-0-later', :camp, 'N', '2026-03-01T00:00:00')"
            ),
            {"camp": campaign_id},
        )

    detail = client.get(f"/api/campaign/{campaign_id}", headers=headers).json()
    assert detail["character_id"] == "char-z-lower-id"
    assert _list_entry(client, headers, campaign_id)["character_id"] == "char-z-lower-id"


def test_character_id_is_owner_scoped() -> None:
    client = _client()
    headers_a = _headers(client, "assoc-owner-a")
    headers_b = _headers(client, "assoc-owner-b")
    campaign_id = _create_campaign(client, headers_a)
    _insert_character(campaign_id, "char-owner-a", "Investigator")

    assert client.get(f"/api/campaign/{campaign_id}", headers=headers_b).status_code == 404
    ids = [c["campaign_id"] for c in client.get("/api/campaigns", headers=headers_b).json()]
    assert campaign_id not in ids
    assert client.get("/api/character/char-owner-a", headers=headers_b).status_code == 404
    assert client.get(f"/api/campaign/{campaign_id}").status_code in (401, 403)
