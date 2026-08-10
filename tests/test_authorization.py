"""Phase 2B: domain authorization and ownership enforcement tests."""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from app.api.dependencies import INTERNAL_USER_ID_HEADER_NAME
from app.core.config import settings
from app.db.session import init_db, session
from app.main import app
from app.schemas.internal_auth import CANONICAL_GOOGLE_ISSUER


def _resolve_user(
    client: TestClient, provider_subject: str
) -> tuple[str, dict[str, str]]:
    """Register a user and return (user_id, auth_headers)."""
    response = client.post(
        "/internal/auth/users/resolve",
        json={
            "identity_provider": "google",
            "provider_issuer": CANONICAL_GOOGLE_ISSUER,
            "provider_subject": provider_subject,
            "email": f"{provider_subject}@example.com",
            "email_verified": True,
            "display_name": f"User {provider_subject}",
            "avatar_url": "https://example.com/avatar.png",
        },
        headers={"Authorization": f"Bearer {settings.INTERNAL_ENGINE_SERVICE_TOKEN}"},
    )
    assert response.status_code == 200
    user_id = response.json()["user_id"]
    headers = {
        "Authorization": f"Bearer {settings.INTERNAL_ENGINE_SERVICE_TOKEN}",
        INTERNAL_USER_ID_HEADER_NAME: user_id,
    }
    return user_id, headers


def _create_campaign(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post("/api/campaign", json={}, headers=headers)
    assert response.status_code == 201
    return response.json()["campaign_id"]


def _setup() -> None:
    settings.INTERNAL_ENGINE_SERVICE_TOKEN = "test-token"
    settings.AI_ENABLED = False
    settings.OPENAI_API_KEY = None


# ---------------------------------------------------------------------------
# Campaign listing
# ---------------------------------------------------------------------------


def test_list_campaigns_returns_only_owned() -> None:
    _setup()
    client = TestClient(app)
    _, headers_a = _resolve_user(client, "list-user-a")
    _, headers_b = _resolve_user(client, "list-user-b")

    _create_campaign(client, headers_a)
    _create_campaign(client, headers_a)
    _create_campaign(client, headers_b)

    response = client.get("/api/campaigns", headers=headers_a)
    assert response.status_code == 200
    campaigns = response.json()
    assert len(campaigns) == 2

    response_b = client.get("/api/campaigns", headers=headers_b)
    assert response_b.status_code == 200
    assert len(response_b.json()) == 1


def test_list_campaigns_does_not_return_other_users_campaigns() -> None:
    _setup()
    client = TestClient(app)
    _, headers_a = _resolve_user(client, "cross-list-a")
    _, headers_b = _resolve_user(client, "cross-list-b")

    campaign_id = _create_campaign(client, headers_a)

    response = client.get("/api/campaigns", headers=headers_b)
    assert response.status_code == 200
    ids = [c["campaign_id"] for c in response.json()]
    assert campaign_id not in ids


def test_list_campaigns_excludes_legacy_unowned(tmp_path) -> None:
    _setup()
    db_path = tmp_path / "legacy_list.db"
    original = settings.DATABASE_URL
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    try:
        with sqlite3.connect(str(db_path)) as conn:
            init_db(conn)
            conn.execute(
                "INSERT INTO campaigns (campaign_id, owner_user_id, name, description, state, created_at) "
                "VALUES (?, NULL, ?, NULL, NULL, ?)",
                ("campaign_legacy_list", "Legacy Campaign", "2024-01-01T00:00:00"),
            )
            conn.commit()

        client = TestClient(app)
        _, headers = _resolve_user(client, "legacy-list-user")

        response = client.get("/api/campaigns", headers=headers)
        assert response.status_code == 200
        ids = [c["campaign_id"] for c in response.json()]
        assert "campaign_legacy_list" not in ids
    finally:
        settings.DATABASE_URL = original


# ---------------------------------------------------------------------------
# Campaign retrieval
# ---------------------------------------------------------------------------


def test_get_campaign_owner_succeeds() -> None:
    _setup()
    client = TestClient(app)
    _, headers = _resolve_user(client, "get-owner")

    campaign_id = _create_campaign(client, headers)

    response = client.get(f"/api/campaign/{campaign_id}", headers=headers)
    assert response.status_code == 200
    assert response.json()["campaign_id"] == campaign_id


def test_get_campaign_other_user_returns_404() -> None:
    _setup()
    client = TestClient(app)
    _, headers_a = _resolve_user(client, "get-other-a")
    _, headers_b = _resolve_user(client, "get-other-b")

    campaign_id = _create_campaign(client, headers_a)

    response = client.get(f"/api/campaign/{campaign_id}", headers=headers_b)
    assert response.status_code == 404


def test_get_campaign_nonexistent_returns_404() -> None:
    _setup()
    client = TestClient(app)
    _, headers = _resolve_user(client, "get-missing")

    response = client.get("/api/campaign/campaign_doesnotexist", headers=headers)
    assert response.status_code == 404


def test_get_campaign_unowned_returns_404(tmp_path) -> None:
    _setup()
    db_path = tmp_path / "legacy_get.db"
    original = settings.DATABASE_URL
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    try:
        with sqlite3.connect(str(db_path)) as conn:
            init_db(conn)
            conn.execute(
                "INSERT INTO campaigns (campaign_id, owner_user_id, name, description, state, created_at) "
                "VALUES (?, NULL, ?, NULL, NULL, ?)",
                ("campaign_legacy_get", "Legacy", "2024-01-01T00:00:00"),
            )
            conn.commit()

        client = TestClient(app)
        _, headers = _resolve_user(client, "legacy-get-user")

        response = client.get("/api/campaign/campaign_legacy_get", headers=headers)
        assert response.status_code == 404
    finally:
        settings.DATABASE_URL = original


def test_get_campaign_missing_and_unauthorized_indistinguishable() -> None:
    """Nonexistent and cross-user campaigns must return the same response shape."""
    _setup()
    client = TestClient(app)
    _, headers_a = _resolve_user(client, "indist-a")
    _, headers_b = _resolve_user(client, "indist-b")

    campaign_id = _create_campaign(client, headers_a)

    r_missing = client.get("/api/campaign/campaign_totally_missing", headers=headers_b)
    r_cross = client.get(f"/api/campaign/{campaign_id}", headers=headers_b)

    assert r_missing.status_code == 404
    assert r_cross.status_code == 404
    assert r_missing.json() == r_cross.json()


def test_get_campaign_response_does_not_leak_owner_id() -> None:
    _setup()
    client = TestClient(app)
    _, headers = _resolve_user(client, "owner-leak-check")

    campaign_id = _create_campaign(client, headers)
    response = client.get(f"/api/campaign/{campaign_id}", headers=headers)

    assert response.status_code == 200
    data = response.json()
    assert "owner_user_id" not in data


# ---------------------------------------------------------------------------
# Campaign deletion
# ---------------------------------------------------------------------------


def test_delete_campaign_owner_succeeds() -> None:
    _setup()
    client = TestClient(app)
    _, headers = _resolve_user(client, "delete-owner")

    campaign_id = _create_campaign(client, headers)

    response = client.delete(f"/api/campaign/{campaign_id}", headers=headers)
    assert response.status_code == 204

    get_response = client.get(f"/api/campaign/{campaign_id}", headers=headers)
    assert get_response.status_code == 404


def test_delete_campaign_other_user_returns_404() -> None:
    _setup()
    client = TestClient(app)
    _, headers_a = _resolve_user(client, "del-cross-a")
    _, headers_b = _resolve_user(client, "del-cross-b")

    campaign_id = _create_campaign(client, headers_a)

    response = client.delete(f"/api/campaign/{campaign_id}", headers=headers_b)
    assert response.status_code == 404


def test_delete_campaign_cross_user_does_not_mutate_record() -> None:
    _setup()
    client = TestClient(app)
    _, headers_a = _resolve_user(client, "del-mutate-a")
    _, headers_b = _resolve_user(client, "del-mutate-b")

    campaign_id = _create_campaign(client, headers_a)

    client.delete(f"/api/campaign/{campaign_id}", headers=headers_b)

    # Original owner can still access the campaign
    response = client.get(f"/api/campaign/{campaign_id}", headers=headers_a)
    assert response.status_code == 200


def test_delete_campaign_unowned_returns_404(tmp_path) -> None:
    _setup()
    db_path = tmp_path / "legacy_del.db"
    original = settings.DATABASE_URL
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    try:
        with sqlite3.connect(str(db_path)) as conn:
            init_db(conn)
            conn.execute(
                "INSERT INTO campaigns (campaign_id, owner_user_id, name, description, state, created_at) "
                "VALUES (?, NULL, ?, NULL, NULL, ?)",
                ("campaign_legacy_del", "Legacy", "2024-01-01T00:00:00"),
            )
            conn.commit()

        client = TestClient(app)
        _, headers = _resolve_user(client, "legacy-del-user")

        response = client.delete("/api/campaign/campaign_legacy_del", headers=headers)
        assert response.status_code == 404

        # Verify record still in DB
        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute(
                "SELECT campaign_id FROM campaigns WHERE campaign_id = ?",
                ("campaign_legacy_del",),
            ).fetchone()
        assert row is not None
    finally:
        settings.DATABASE_URL = original


# ---------------------------------------------------------------------------
# Campaign creation
# ---------------------------------------------------------------------------


def test_create_campaign_assigns_owner_from_auth_context() -> None:
    _setup()
    client = TestClient(app)
    user_id, headers = _resolve_user(client, "create-owner-check")

    response = client.post("/api/campaign", json={}, headers=headers)
    assert response.status_code == 201

    campaign_id = response.json()["campaign_id"]
    with session() as db:
        campaign = db.get_campaign(campaign_id)
    assert campaign is not None
    assert campaign.owner_user_id == user_id


# ---------------------------------------------------------------------------
# Chat / gameplay authorization
# ---------------------------------------------------------------------------


def test_chat_owner_can_play_own_campaign() -> None:
    _setup()
    client = TestClient(app)
    _, headers = _resolve_user(client, "chat-play-owner")

    campaign_id = _create_campaign(client, headers)

    response = client.post(
        "/api/chat",
        json={"message": "look around", "campaign_id": campaign_id},
        headers=headers,
    )
    assert response.status_code == 200


def test_chat_other_user_receives_404() -> None:
    _setup()
    client = TestClient(app)
    _, headers_a = _resolve_user(client, "chat-other-a")
    _, headers_b = _resolve_user(client, "chat-other-b")

    campaign_id = _create_campaign(client, headers_a)

    response = client.post(
        "/api/chat",
        json={"message": "look around", "campaign_id": campaign_id},
        headers=headers_b,
    )
    assert response.status_code == 404


def test_chat_unauthorized_creates_no_turn_or_event() -> None:
    _setup()
    client = TestClient(app)
    _, headers_a = _resolve_user(client, "no-side-effect-a")
    _, headers_b = _resolve_user(client, "no-side-effect-b")

    campaign_id = _create_campaign(client, headers_a)

    # Count initial turns/events for campaign_a
    with session() as db:
        _, turns_before, _ = db.get_campaign_with_turns(campaign_id, limit=100)
        events_before = db.list_campaign_events(campaign_id, limit=100)

    client.post(
        "/api/chat",
        json={"message": "unauthorized action", "campaign_id": campaign_id},
        headers=headers_b,
    )

    with session() as db:
        _, turns_after, _ = db.get_campaign_with_turns(campaign_id, limit=100)
        events_after = db.list_campaign_events(campaign_id, limit=100)

    assert len(turns_after) == len(turns_before)
    assert len(events_after) == len(events_before)


def test_chat_unauthorized_does_not_consume_usage_quota() -> None:
    _setup()
    client = TestClient(app)
    _, headers_a = _resolve_user(client, "quota-owner-a")
    user_id_b, headers_b = _resolve_user(client, "quota-attacker-b")

    campaign_id = _create_campaign(client, headers_a)

    with session() as db:
        requests_before = db.count_user_requests_since(user_id_b, "2000-01-01T00:00:00")

    client.post(
        "/api/chat",
        json={"message": "steal quota", "campaign_id": campaign_id},
        headers=headers_b,
    )

    with session() as db:
        requests_after = db.count_user_requests_since(user_id_b, "2000-01-01T00:00:00")

    assert requests_after == requests_before


def test_chat_unowned_campaign_returns_404(tmp_path) -> None:
    _setup()
    db_path = tmp_path / "legacy_chat.db"
    original = settings.DATABASE_URL
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    try:
        with sqlite3.connect(str(db_path)) as conn:
            init_db(conn)
            conn.execute(
                "INSERT INTO campaigns (campaign_id, owner_user_id, name, description, state, created_at) "
                "VALUES (?, NULL, ?, NULL, NULL, ?)",
                ("campaign_legacy_chat", "Legacy", "2024-01-01T00:00:00"),
            )
            conn.commit()

        client = TestClient(app)
        _, headers = _resolve_user(client, "legacy-chat-user")

        response = client.post(
            "/api/chat",
            json={"message": "hello", "campaign_id": "campaign_legacy_chat"},
            headers=headers,
        )
        assert response.status_code == 404
    finally:
        settings.DATABASE_URL = original


# ---------------------------------------------------------------------------
# Repository-level ownership scoping
# ---------------------------------------------------------------------------


def test_repo_list_campaigns_for_owner_excludes_other_users() -> None:
    _setup()
    client = TestClient(app)
    user_id_a, headers_a = _resolve_user(client, "repo-list-a")
    user_id_b, headers_b = _resolve_user(client, "repo-list-b")

    campaign_a = _create_campaign(client, headers_a)
    campaign_b = _create_campaign(client, headers_b)

    with session() as db:
        results_a = db.list_campaigns_for_owner(user_id_a)
        results_b = db.list_campaigns_for_owner(user_id_b)

    ids_a = [r["campaign_id"] for r in results_a]
    ids_b = [r["campaign_id"] for r in results_b]

    assert campaign_a in ids_a
    assert campaign_b not in ids_a
    assert campaign_b in ids_b
    assert campaign_a not in ids_b


def test_repo_get_campaign_for_owner_excludes_null_owner(tmp_path) -> None:
    _setup()
    db_path = tmp_path / "legacy_repo.db"
    original = settings.DATABASE_URL
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    try:
        with sqlite3.connect(str(db_path)) as conn:
            init_db(conn)
            conn.execute(
                "INSERT INTO campaigns (campaign_id, owner_user_id, name, description, state, created_at) "
                "VALUES (?, NULL, ?, NULL, NULL, ?)",
                ("campaign_null_owner", "Null Owned", "2024-01-01T00:00:00"),
            )
            conn.commit()

        with session() as db:
            result = db.get_campaign_for_owner("campaign_null_owner", "user_anyuser")
        assert result is None
    finally:
        settings.DATABASE_URL = original


def test_repo_delete_campaign_for_owner_returns_false_for_wrong_owner() -> None:
    _setup()
    client = TestClient(app)
    _, headers_a = _resolve_user(client, "repo-del-a")
    user_id_b, _ = _resolve_user(client, "repo-del-b")

    campaign_id = _create_campaign(client, headers_a)

    with session() as db:
        deleted = db.delete_campaign_for_owner(campaign_id, user_id_b)
    assert deleted is False

    # Campaign still exists
    with session() as db:
        still_exists = db.get_campaign(campaign_id)
    assert still_exists is not None


def test_repo_count_owner_campaigns_does_not_count_other_users() -> None:
    _setup()
    client = TestClient(app)
    user_id_a, headers_a = _resolve_user(client, "repo-count-a")
    user_id_b, headers_b = _resolve_user(client, "repo-count-b")

    _create_campaign(client, headers_a)
    _create_campaign(client, headers_a)
    _create_campaign(client, headers_b)

    with session() as db:
        count_a = db.count_owner_campaigns(user_id_a)
        count_b = db.count_owner_campaigns(user_id_b)

    assert count_a == 2
    assert count_b == 1


# ---------------------------------------------------------------------------
# Security: IDOR and bypass attempts
# ---------------------------------------------------------------------------


def test_request_body_owner_cannot_bypass_ownership() -> None:
    """An attacker injecting owner_user_id in the request body must not gain access."""
    _setup()
    client = TestClient(app)
    user_id_a, headers_a = _resolve_user(client, "idor-body-a")
    _, headers_b = _resolve_user(client, "idor-body-b")

    campaign_id = _create_campaign(client, headers_a)

    # Attacker sends victim owner_user_id in body - not a supported field but verifying it's ignored
    response = client.post(
        "/api/chat",
        json={
            "message": "attack",
            "campaign_id": campaign_id,
            "owner_user_id": user_id_a,
        },
        headers=headers_b,
    )
    assert response.status_code == 422


def test_campaign_id_in_url_cannot_access_other_users_campaign() -> None:
    """Changing campaign ID in the URL to another user's campaign must return 404."""
    _setup()
    client = TestClient(app)
    _, headers_a = _resolve_user(client, "idor-url-a")
    _, headers_b = _resolve_user(client, "idor-url-b")

    campaign_id_a = _create_campaign(client, headers_a)

    response = client.get(f"/api/campaign/{campaign_id_a}", headers=headers_b)
    assert response.status_code == 404

    response = client.delete(f"/api/campaign/{campaign_id_a}", headers=headers_b)
    assert response.status_code == 404


def test_service_only_endpoint_still_works() -> None:
    """Phase 1C service-only endpoints must continue working."""
    _setup()
    client = TestClient(app)

    response = client.post(
        "/internal/auth/users/resolve",
        json={
            "identity_provider": "google",
            "provider_issuer": CANONICAL_GOOGLE_ISSUER,
            "provider_subject": "service-still-works",
            "email": "service-still-works@example.com",
            "email_verified": True,
            "display_name": None,
            "avatar_url": None,
        },
        headers={"Authorization": f"Bearer {settings.INTERNAL_ENGINE_SERVICE_TOKEN}"},
    )
    assert response.status_code == 200
    assert "user_id" in response.json()


def test_health_endpoint_still_public() -> None:
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Memory isolation
# ---------------------------------------------------------------------------


def test_memory_search_scoped_to_campaign() -> None:
    """Memory from user A's campaign must not appear in user B's context."""
    _setup()
    client = TestClient(app)
    user_id_a, headers_a = _resolve_user(client, "mem-scope-a")
    user_id_b, headers_b = _resolve_user(client, "mem-scope-b")

    campaign_a = _create_campaign(client, headers_a)

    # Add a memory directly to campaign_a
    with session() as db:
        db.add_memory(
            campaign_a,
            "message",
            "User A secret memory content",
            importance=1.0,
        )

    # User B searches in their own campaign - must not see user A's memory
    campaign_b = _create_campaign(client, headers_b)
    with session() as db:
        results = db.search_campaign_memories(
            campaign_b, "secret memory content", limit=10
        )

    assert all(m.campaign_id == campaign_b for m in results)
    assert not any("User A secret memory content" in m.content for m in results)
