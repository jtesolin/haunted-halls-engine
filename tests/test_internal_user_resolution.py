from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from fastapi.testclient import TestClient

from app.main import app
from app.core.config import settings
from app.db.session import session
from app.schemas.internal_auth import CANONICAL_GOOGLE_ISSUER


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.INTERNAL_ENGINE_SERVICE_TOKEN or ''}"}


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "identity_provider": "google",
        "provider_issuer": CANONICAL_GOOGLE_ISSUER,
        "provider_subject": "google-sub-123",
        "email": "player@example.com",
        "email_verified": True,
        "display_name": "Player One",
        "avatar_url": "https://example.com/avatar.png",
    }
    payload.update(overrides)
    return payload


def _resolve(client: TestClient, payload: dict[str, object]):
    return client.post(
        "/internal/auth/users/resolve",
        json=payload,
        headers=_auth_headers(),
    )


def test_resolution_requires_internal_service_auth() -> None:
    client = TestClient(app)
    response = client.post("/internal/auth/users/resolve", json=_payload())
    assert response.status_code == 401


def test_first_valid_request_creates_user_and_returns_user_id_only() -> None:
    client = TestClient(app)
    response = _resolve(client, _payload())

    assert response.status_code == 200
    data = response.json()
    assert list(data.keys()) == ["user_id"]
    assert data["user_id"].startswith("user_")


def test_repeat_resolution_returns_same_internal_user_id() -> None:
    client = TestClient(app)
    first = _resolve(client, _payload())
    second = _resolve(client, _payload())

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["user_id"] == second.json()["user_id"]


def test_repeat_login_updates_mutable_profile_fields_and_last_login() -> None:
    client = TestClient(app)
    initial = _resolve(client, _payload())
    assert initial.status_code == 200

    with session() as db:
        before = db.get_internal_user_by_identity(CANONICAL_GOOGLE_ISSUER, "google-sub-123")
        assert before is not None
        before_login = before.last_login_at

    updated = _resolve(
        client,
        _payload(
            email="updated@example.com",
            display_name="Updated Name",
            avatar_url="https://example.com/new-avatar.png",
        ),
    )
    assert updated.status_code == 200

    with session() as db:
        after = db.get_internal_user_by_identity(CANONICAL_GOOGLE_ISSUER, "google-sub-123")
        assert after is not None
        assert after.id == initial.json()["user_id"]
        assert after.email == "updated@example.com"
        assert after.display_name == "Updated Name"
        assert after.avatar_url == "https://example.com/new-avatar.png"
        assert after.last_login_at > before_login


def test_changing_email_does_not_create_new_user() -> None:
    client = TestClient(app)
    first = _resolve(client, _payload(email="first@example.com"))
    second = _resolve(client, _payload(email="second@example.com"))

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["user_id"] == second.json()["user_id"]


def test_same_email_with_different_subject_creates_distinct_user() -> None:
    client = TestClient(app)
    first = _resolve(client, _payload(provider_subject="sub-1"))
    second = _resolve(client, _payload(provider_subject="sub-2"))

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["user_id"] != second.json()["user_id"]


def test_same_subject_with_different_issuer_does_not_match_when_issuer_not_supported() -> None:
    client = TestClient(app)
    first = _resolve(client, _payload(provider_subject="sub-issuer-check"))
    invalid = _resolve(client, _payload(provider_subject="sub-issuer-check", provider_issuer="https://evil.example"))

    assert first.status_code == 200
    assert invalid.status_code == 422


def test_unverified_email_is_rejected() -> None:
    client = TestClient(app)
    response = _resolve(client, _payload(email_verified=False))
    assert response.status_code == 422


def test_blank_subject_is_rejected() -> None:
    client = TestClient(app)
    response = _resolve(client, _payload(provider_subject="   "))
    assert response.status_code == 422


def test_unsupported_provider_is_rejected() -> None:
    client = TestClient(app)
    response = _resolve(client, _payload(identity_provider="github"))
    assert response.status_code == 422


def test_unsupported_issuer_is_rejected() -> None:
    client = TestClient(app)
    response = _resolve(client, _payload(provider_issuer="https://issuer.example"))
    assert response.status_code == 422


def test_google_issuer_forms_canonicalize_to_one_identity() -> None:
    client = TestClient(app)
    first = _resolve(client, _payload(provider_issuer="accounts.google.com", provider_subject="canonical-sub"))
    second = _resolve(client, _payload(provider_issuer=CANONICAL_GOOGLE_ISSUER, provider_subject="canonical-sub"))

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["user_id"] == second.json()["user_id"]


def test_unexpected_fields_are_rejected() -> None:
    client = TestClient(app)
    payload = _payload(id_token="token-value")
    response = _resolve(client, payload)
    assert response.status_code == 422


def test_no_provider_tokens_or_raw_claims_are_persisted() -> None:
    client = TestClient(app)
    response = _resolve(client, _payload(provider_subject="persist-check"))
    assert response.status_code == 200

    with session() as db:
        row = db.conn.execute(
            "SELECT * FROM internal_users WHERE provider_subject = ?",
            ("persist-check",),
        ).fetchone()
        assert row is not None
        serialized = json.dumps(dict(row))
        assert "id_token" not in serialized
        assert "access_token" not in serialized
        assert "refresh_token" not in serialized
        assert "claims" not in serialized


def test_duplicate_creation_race_returns_existing_user(monkeypatch) -> None:
    client = TestClient(app)

    with session() as db:
        original_insert = db._insert_internal_user
        race_triggered = {"value": False}

        def race_insert(*args, **kwargs):
            if not race_triggered["value"]:
                race_triggered["value"] = True
                original_insert(*args, **kwargs)
                raise sqlite3.IntegrityError("UNIQUE constraint failed")
            return original_insert(*args, **kwargs)

        monkeypatch.setattr(db, "_insert_internal_user", race_insert)
        user = db.resolve_internal_user(
            identity_provider="google",
            provider_issuer=CANONICAL_GOOGLE_ISSUER,
            provider_subject="race-subject",
            email="race@example.com",
            email_verified=True,
            display_name="Race User",
            avatar_url=None,
        )

        assert user.id.startswith("user_")


def test_created_and_updated_timestamps_are_timezone_aware_utc() -> None:
    client = TestClient(app)
    response = _resolve(client, _payload(provider_subject="tz-subject"))
    assert response.status_code == 200

    with session() as db:
        user = db.get_internal_user_by_identity(CANONICAL_GOOGLE_ISSUER, "tz-subject")
        assert user is not None
        for value in [user.created_at, user.updated_at, user.last_login_at]:
            assert value.tzinfo is not None
            assert value.utcoffset() is not None
            assert datetime.fromisoformat(value.isoformat()).tzinfo is not None
