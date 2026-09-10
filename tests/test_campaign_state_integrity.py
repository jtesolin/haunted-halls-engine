from __future__ import annotations

import json
import logging
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.dependencies import INTERNAL_USER_ID_HEADER_NAME
from app.core.config import settings
from app.db.session import session
from app.game.campaign_state import InvalidCampaignStateError
from app.main import app
from app.schemas.chat import ActionType, ParsedAction
from app.schemas.internal_auth import CANONICAL_GOOGLE_ISSUER
from app.services.tool_executor import ToolExecutor


def _build_executor() -> ToolExecutor:
    return ToolExecutor()


def _move_action() -> ParsedAction:
    return ParsedAction(raw_text="go north", action=ActionType.MOVE, target="north", parse_status="ok")


# ---------------------------------------------------------------------------
# ToolExecutor._state_from_text regression coverage
# ---------------------------------------------------------------------------


def test_state_from_text_empty_uses_fresh_state() -> None:
    executor = _build_executor()
    state = executor._state_from_text("")
    assert state["player"]["location"] == "entry_hall"


def test_state_from_text_missing_sentinel_uses_fresh_state() -> None:
    executor = _build_executor()
    state = executor._state_from_text("No campaign state yet.")
    assert state["player"]["location"] == "entry_hall"


def test_state_from_text_valid_dict_json_loads_and_normalizes() -> None:
    executor = _build_executor()
    payload = json.dumps({"player": {"location": "library", "inventory": []}})
    state = executor._state_from_text(payload)
    assert state["player"]["location"] == "library"
    assert "items" in state
    assert "npcs" in state


@pytest.mark.parametrize(
    "malformed_payload",
    [
        "{not valid json",
        "[]",
        '"just a string"',
        "42",
        "null",
    ],
)
def test_state_from_text_malformed_or_non_object_raises(malformed_payload: str) -> None:
    executor = _build_executor()
    with patch(
        "app.game.campaign_state.build_fresh_campaign_state",
        side_effect=AssertionError("build_fresh_campaign_state must not be called"),
    ):
        with pytest.raises(InvalidCampaignStateError):
            executor._state_from_text(malformed_payload)


def test_state_from_text_malformed_does_not_call_fresh_state_via_execute() -> None:
    executor = _build_executor()
    with patch(
        "app.game.campaign_state.build_fresh_campaign_state",
        side_effect=AssertionError("build_fresh_campaign_state must not be called"),
    ):
        with pytest.raises(InvalidCampaignStateError):
            executor.execute(parsed_action=_move_action(), campaign_state="{bad json")


# ---------------------------------------------------------------------------
# Orchestrator / persistence integrity coverage
# ---------------------------------------------------------------------------


def _user_scoped_headers(client: TestClient, provider_subject: str) -> dict[str, str]:
    auth_token = settings.INTERNAL_ENGINE_SERVICE_TOKEN or ""
    resolve_response = client.post(
        "/internal/auth/users/resolve",
        json={
            "identity_provider": "google",
            "provider_issuer": CANONICAL_GOOGLE_ISSUER,
            "provider_subject": provider_subject,
            "email": f"{provider_subject}@example.com",
            "email_verified": True,
            "display_name": "Test Player",
            "avatar_url": "https://example.com/avatar.png",
        },
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert resolve_response.status_code == 200
    user_id = resolve_response.json()["user_id"]
    return {
        "Authorization": f"Bearer {auth_token}",
        INTERNAL_USER_ID_HEADER_NAME: user_id,
    }


def _corrupt_campaign_state(campaign_id: str, corrupted_state: str) -> None:
    with session() as db:
        db.conn.execute(
            text("UPDATE campaigns SET state = :state WHERE campaign_id = :campaign_id"),
            {"state": corrupted_state, "campaign_id": campaign_id},
        )


def test_chat_fails_with_500_on_corrupted_persisted_state(caplog: Any) -> None:
    settings.INTERNAL_ENGINE_SERVICE_TOKEN = "test-token"
    settings.AI_ENABLED = False
    settings.OPENAI_API_KEY = None
    client = TestClient(app)
    headers = _user_scoped_headers(client, "corrupt-state-user")

    # Create the campaign with legitimate initial state first.
    first_response = client.post(
        "/api/chat",
        json={"message": "look around"},
        headers=headers,
    )
    assert first_response.status_code == 200
    campaign_id = first_response.json()["campaign_id"]

    # Sanity check: capture committed turn/event counts before the failing request.
    with session() as db:
        turns_before = db.conn.execute(
            text("SELECT COUNT(*) FROM turns WHERE campaign_id = :campaign_id"),
            {"campaign_id": campaign_id},
        ).scalar_one()
        events_before = db.conn.execute(
            text("SELECT COUNT(*) FROM game_events WHERE campaign_id = :campaign_id"),
            {"campaign_id": campaign_id},
        ).scalar_one()
        state_before = db.conn.execute(
            text("SELECT state FROM campaigns WHERE campaign_id = :campaign_id"),
            {"campaign_id": campaign_id},
        ).scalar_one()

    # Corrupt the persisted state directly, simulating data corruption.
    _corrupt_campaign_state(campaign_id, "{not valid json at all")

    narrator_called = {"count": 0}

    async def _fail_if_called(*args: Any, **kwargs: Any) -> str:
        narrator_called["count"] += 1
        raise AssertionError("narrator must not be called after integrity failure")

    caplog.set_level(logging.INFO)
    logging.getLogger("app.orchestration.orchestrator").disabled = False
    with patch(
        "app.orchestration.orchestrator.ChatOrchestrator._generate_narrator_response",
        new=_fail_if_called,
    ):
        response = client.post(
            "/api/chat",
            json={"message": "look around", "campaign_id": campaign_id},
            headers=headers,
        )

    assert response.status_code == 500
    assert narrator_called["count"] == 0

    # No raw corrupted payload should appear in logs.
    for record in caplog.records:
        assert "not valid json at all" not in record.getMessage()
    assert any("campaign_state_integrity_failure" in record.getMessage() for record in caplog.records)

    with session() as db:
        turns_after = db.conn.execute(
            text("SELECT COUNT(*) FROM turns WHERE campaign_id = :campaign_id"),
            {"campaign_id": campaign_id},
        ).scalar_one()
        events_after = db.conn.execute(
            text("SELECT COUNT(*) FROM game_events WHERE campaign_id = :campaign_id"),
            {"campaign_id": campaign_id},
        ).scalar_one()
        state_after = db.conn.execute(
            text("SELECT state FROM campaigns WHERE campaign_id = :campaign_id"),
            {"campaign_id": campaign_id},
        ).scalar_one()

    # Sanity: prove the state before corruption injection was legitimate JSON.
    assert json.loads(state_before)["player"]["location"] == "entry_hall"

    # The failed request must not have committed a new player/assistant turn or event,
    # and the corrupted persisted state must remain exactly as-injected (not silently
    # replaced by a freshly generated campaign state).
    assert turns_after == turns_before
    assert events_after == events_before
    assert state_after == "{not valid json at all"


def test_chat_fails_with_500_on_corrupted_state_even_when_parse_status_ambiguous(
    caplog: Any,
) -> None:
    """Corrupted persisted state must fail before parser/narrator paths, even
    when the parsed action status is non-`ok` (e.g. `ambiguous`) and would
    otherwise bypass `ToolExecutor._state_from_text()` entirely."""
    settings.INTERNAL_ENGINE_SERVICE_TOKEN = "test-token"
    settings.AI_ENABLED = False
    settings.OPENAI_API_KEY = None
    client = TestClient(app)
    headers = _user_scoped_headers(client, "corrupt-state-ambiguous-user")

    first_response = client.post(
        "/api/chat",
        json={"message": "look around"},
        headers=headers,
    )
    assert first_response.status_code == 200
    campaign_id = first_response.json()["campaign_id"]

    with session() as db:
        turns_before = db.conn.execute(
            text("SELECT COUNT(*) FROM turns WHERE campaign_id = :campaign_id"),
            {"campaign_id": campaign_id},
        ).scalar_one()
        events_before = db.conn.execute(
            text("SELECT COUNT(*) FROM game_events WHERE campaign_id = :campaign_id"),
            {"campaign_id": campaign_id},
        ).scalar_one()

    _corrupt_campaign_state(campaign_id, "{not valid json at all")

    narrator_called = {"count": 0}

    async def _fail_if_narrator_called(*args: Any, **kwargs: Any) -> str:
        narrator_called["count"] += 1
        raise AssertionError("narrator must not be called after integrity failure")

    async def _ambiguous_parse(*args: Any, **kwargs: Any) -> ParsedAction:
        return ParsedAction(
            raw_text="do the thing",
            action=ActionType.MOVE,
            target=None,
            parse_status="ambiguous",
            parser_notes="ambiguous test action",
        )

    caplog.set_level(logging.INFO)
    logging.getLogger("app.orchestration.orchestrator").disabled = False
    with patch(
        "app.orchestration.orchestrator.ChatOrchestrator._generate_narrator_response",
        new=_fail_if_narrator_called,
    ), patch(
        "app.agents.action_parser.ActionParserAgent.parse",
        new=_ambiguous_parse,
    ):
        response = client.post(
            "/api/chat",
            json={"message": "do the thing", "campaign_id": campaign_id},
            headers=headers,
        )

    assert response.status_code == 500
    assert narrator_called["count"] == 0

    for record in caplog.records:
        assert "not valid json at all" not in record.getMessage()
    assert any(
        "campaign_state_integrity_failure" in record.getMessage()
        for record in caplog.records
    )

    with session() as db:
        turns_after = db.conn.execute(
            text("SELECT COUNT(*) FROM turns WHERE campaign_id = :campaign_id"),
            {"campaign_id": campaign_id},
        ).scalar_one()
        events_after = db.conn.execute(
            text("SELECT COUNT(*) FROM game_events WHERE campaign_id = :campaign_id"),
            {"campaign_id": campaign_id},
        ).scalar_one()
        state_after = db.conn.execute(
            text("SELECT state FROM campaigns WHERE campaign_id = :campaign_id"),
            {"campaign_id": campaign_id},
        ).scalar_one()

    assert turns_after == turns_before
    assert events_after == events_before
    assert state_after == "{not valid json at all"


def test_chat_ownership_unaffected_by_corruption_handling() -> None:
    settings.INTERNAL_ENGINE_SERVICE_TOKEN = "test-token"
    settings.AI_ENABLED = False
    settings.OPENAI_API_KEY = None
    client = TestClient(app)
    owner_headers = _user_scoped_headers(client, "owner-user-corrupt")
    other_headers = _user_scoped_headers(client, "other-user-corrupt")

    create_response = client.post(
        "/api/chat",
        json={"message": "look around"},
        headers=owner_headers,
    )
    assert create_response.status_code == 200
    campaign_id = create_response.json()["campaign_id"]

    _corrupt_campaign_state(campaign_id, "[]")

    # A different user attempting to use this campaign_id should still be rejected
    # by ownership checks rather than reaching the corrupted-state handling path.
    other_response = client.post(
        "/api/chat",
        json={"message": "look around", "campaign_id": campaign_id},
        headers=other_headers,
    )
    assert other_response.status_code in (403, 404)
