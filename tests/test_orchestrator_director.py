"""Phase 7B3 acceptance coverage: Director integration inside ChatOrchestrator.

These tests exercise `ChatOrchestrator.handle_chat` with the model-backed
`DirectorAgent` and `WorldAuthorityExecutor` wired into the normal turn, using
mocked Director/model calls only. No real provider network calls are made.
"""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.agents.director import (
    DirectorAgentResult,
    DirectorProposalOutputError,
    DirectorProviderError,
)
from app.agents.narrator import NarratorAgentOutput
from app.ai.model_client import ModelUsage
from app.api.dependencies import INTERNAL_USER_ID_HEADER_NAME
from app.core.config import settings
from app.db.session import session
from app.game.campaign_state import build_fresh_campaign_state
from app.main import app
from app.memory.services import MemoryService
from app.orchestration import orchestrator as orchestrator_module
from app.schemas.chat import ActionType, ChatRequest, ParsedAction
from app.schemas.director import NoActionProposal
from app.schemas.internal_auth import CANONICAL_GOOGLE_ISSUER
from app.schemas.world import MoveNpcWorldAction, SetNpcStatusWorldAction


def _enable_provider() -> None:
    settings.INTERNAL_ENGINE_SERVICE_TOKEN = "test-token"
    settings.AI_ENABLED = True
    settings.OPENAI_API_KEY = "test-key"


def _resolve_user(client: TestClient, provider_subject: str) -> tuple[dict[str, str], str]:
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
            "avatar_url": None,
        },
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert resolve_response.status_code == 200
    user_id = resolve_response.json()["user_id"]
    return (
        {
            "Authorization": f"Bearer {auth_token}",
            INTERNAL_USER_ID_HEADER_NAME: user_id,
        },
        user_id,
    )


async def _fake_parse_move_north(**kwargs) -> ParsedAction:  # noqa: ANN003, ARG001
    return ParsedAction(
        raw_text="I go north.",
        action=ActionType.MOVE,
        target="north",
        confidence=0.95,
        parse_status="ok",
    )


async def _fake_parse_ambiguous(**kwargs) -> ParsedAction:  # noqa: ANN003, ARG001
    return ParsedAction(
        raw_text="hmm",
        action=ActionType.UNKNOWN,
        confidence=0.3,
        parse_status="ambiguous",
    )


async def _fake_narrator_generate(*, payload, model=None):  # noqa: ANN001, ARG001, ANN202
    return NarratorAgentOutput(reply_text="A haunted reply")


def _install_move_and_narrator_stubs(monkeypatch) -> None:
    monkeypatch.setattr(
        orchestrator_module.orchestrator.action_parser_agent,
        "parse",
        _fake_parse_move_north,
    )
    monkeypatch.setattr(
        orchestrator_module.orchestrator.narrator_agent,
        "generate",
        _fake_narrator_generate,
    )


def _no_action_result() -> DirectorAgentResult:
    return DirectorAgentResult(proposal=NoActionProposal(), usage=None)


def test_director_invoked_once_with_post_player_state_projection(monkeypatch) -> None:
    _enable_provider()
    _install_move_and_narrator_stubs(monkeypatch)

    calls = []

    async def spy_propose(*, director_input, model=None):  # noqa: ANN001, ARG001, ANN202
        calls.append(director_input)
        return _no_action_result()

    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", spy_propose)

    client = TestClient(app)
    _headers, user_id = _resolve_user(client, "director-invoked-once")

    response = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="I go north."), owner_user_id=user_id
        )
    )

    assert response.reply == "A haunted reply"
    assert len(calls) == 1
    director_input = calls[0]
    assert director_input.current_player_room_id == "grand_corridor"
    assert director_input.player_action.action == ActionType.MOVE
    assert director_input.player_action.succeeded is True


def test_provider_disabled_skips_director_and_world_authority() -> None:
    settings.AI_ENABLED = False
    settings.OPENAI_API_KEY = None

    def fail_if_called_propose(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("Director must not be invoked without a provider model.")

    def fail_if_called_execute(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError(
            "WorldAuthorityExecutor must not execute without a provider model."
        )

    orchestrator_instance = orchestrator_module.orchestrator
    original_propose = orchestrator_instance.director_agent.propose
    original_execute = orchestrator_instance.world_authority_executor.execute
    orchestrator_instance.director_agent.propose = fail_if_called_propose
    orchestrator_instance.world_authority_executor.execute = fail_if_called_execute
    try:
        client = TestClient(app)
        _headers, user_id = _resolve_user(client, "director-provider-disabled")

        response = asyncio.run(
            orchestrator_instance.handle_chat(
                ChatRequest(message="I go north."), owner_user_id=user_id
            )
        )
    finally:
        orchestrator_instance.director_agent.propose = original_propose
        orchestrator_instance.world_authority_executor.execute = original_execute

    assert response.reply == "AI narrator replies (stub): I go north."

    with session() as db:
        events = db.list_campaign_events(response.campaign_id)
        assert "world_action_executed" not in {event.type for event in events}
        assert "world_action_failed" not in {event.type for event in events}


def test_no_action_decision_does_not_invoke_world_authority_or_mutate_state(
    monkeypatch,
) -> None:
    _enable_provider()
    _install_move_and_narrator_stubs(monkeypatch)

    async def fake_propose(*, director_input, model=None):  # noqa: ANN001, ARG001, ANN202
        return _no_action_result()

    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", fake_propose)

    executor_calls = []
    original_execute = orchestrator_module.orchestrator.world_authority_executor.execute

    def spy_execute(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        executor_calls.append((args, kwargs))
        return original_execute(*args, **kwargs)

    monkeypatch.setattr(
        orchestrator_module.orchestrator.world_authority_executor, "execute", spy_execute
    )

    client = TestClient(app)
    _headers, user_id = _resolve_user(client, "director-no-action")

    response = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="I go north."), owner_user_id=user_id
        )
    )

    assert executor_calls == []
    with session() as db:
        events = db.list_campaign_events(response.campaign_id)
        assert [event.type for event in events] == [
            "player_message_received",
            "action_parsed",
            "tool_executed",
            "game_state_updated",
            "narrator_response_created",
        ]


def test_world_action_executes_exactly_once_and_grounds_narrator_with_npc_presence(
    monkeypatch,
) -> None:
    _enable_provider()
    _install_move_and_narrator_stubs(monkeypatch)

    move_npc_action = MoveNpcWorldAction(
        npc_id="old_caretaker", destination_room_id="grand_corridor"
    )

    from app.schemas.director import WorldActionProposal

    async def fake_propose(*, director_input, model=None):  # noqa: ANN001, ARG001, ANN202
        return DirectorAgentResult(
            proposal=WorldActionProposal(world_action=move_npc_action), usage=None
        )

    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", fake_propose)

    tool_executor_calls = []
    original_tool_execute = orchestrator_module.orchestrator.tool_executor.execute

    def spy_tool_execute(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        tool_executor_calls.append((args, kwargs))
        return original_tool_execute(*args, **kwargs)

    monkeypatch.setattr(
        orchestrator_module.orchestrator.tool_executor, "execute", spy_tool_execute
    )

    world_executor_calls = []
    original_world_execute = orchestrator_module.orchestrator.world_authority_executor.execute

    def spy_world_execute(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        world_executor_calls.append((args, kwargs))
        return original_world_execute(*args, **kwargs)

    monkeypatch.setattr(
        orchestrator_module.orchestrator.world_authority_executor, "execute", spy_world_execute
    )

    captured_scene = {}

    async def capturing_narrator_generate(*, payload, model=None):  # noqa: ANN001, ARG001, ANN202
        captured_scene["scene"] = payload.scene_context
        return NarratorAgentOutput(reply_text="A haunted reply")

    monkeypatch.setattr(
        orchestrator_module.orchestrator.narrator_agent, "generate", capturing_narrator_generate
    )

    client = TestClient(app)
    _headers, user_id = _resolve_user(client, "director-world-action-exec")

    response = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="I go north."), owner_user_id=user_id
        )
    )

    # Exactly one player tool execution, exactly one privileged world execution.
    assert len(tool_executor_calls) == 1
    assert len(world_executor_calls) == 1
    assert world_executor_calls[0][0][0] == move_npc_action

    with session() as db:
        campaign = db.get_campaign(response.campaign_id)
        assert campaign is not None
        assert campaign.state is not None
        final_state = json.loads(campaign.state)
        assert final_state["npcs"]["old_caretaker"]["location"] == "grand_corridor"

        events = db.list_campaign_events(response.campaign_id)
        assert [event.type for event in events] == [
            "player_message_received",
            "action_parsed",
            "tool_executed",
            "game_state_updated",
            "world_action_executed",
            "game_state_updated",
            "narrator_response_created",
        ]
        world_event_payload = json.loads(events[-3].payload_json or "{}")
        assert world_event_payload["action"] == "move_npc"
        assert world_event_payload["changed"] is True

    scene = captured_scene["scene"]
    assert scene.current_room.id == "grand_corridor"
    assert any(npc.id == "old_caretaker" for npc in scene.nearby_npcs)


def test_successful_no_op_world_action_records_event_without_state_replacement(
    monkeypatch,
) -> None:
    _enable_provider()
    _install_move_and_narrator_stubs(monkeypatch)

    from app.schemas.director import WorldActionProposal

    no_op_action = SetNpcStatusWorldAction(npc_id="old_caretaker", status="active")

    async def fake_propose(*, director_input, model=None):  # noqa: ANN001, ARG001, ANN202
        return DirectorAgentResult(
            proposal=WorldActionProposal(world_action=no_op_action), usage=None
        )

    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", fake_propose)

    client = TestClient(app)
    _headers, user_id = _resolve_user(client, "director-world-action-noop")

    response = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="I go north."), owner_user_id=user_id
        )
    )

    with session() as db:
        events = db.list_campaign_events(response.campaign_id)
        event_types = [event.type for event in events]
        # No-op world action: dedicated execution event, but no extra
        # game_state_updated beyond the player move's own state write.
        assert event_types == [
            "player_message_received",
            "action_parsed",
            "tool_executed",
            "game_state_updated",
            "world_action_executed",
            "narrator_response_created",
        ]
        world_event_payload = json.loads(events[-2].payload_json or "{}")
        assert world_event_payload["changed"] is False


def test_semantic_world_action_failure_leaves_state_unchanged_and_completes_turn(
    monkeypatch,
) -> None:
    _enable_provider()
    _install_move_and_narrator_stubs(monkeypatch)

    from app.schemas.director import WorldActionProposal

    unreachable_action = MoveNpcWorldAction(
        npc_id="old_caretaker", destination_room_id="crypt"
    )

    async def fake_propose(*, director_input, model=None):  # noqa: ANN001, ARG001, ANN202
        return DirectorAgentResult(
            proposal=WorldActionProposal(world_action=unreachable_action), usage=None
        )

    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", fake_propose)

    client = TestClient(app)
    _headers, user_id = _resolve_user(client, "director-world-action-failure")

    response = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="I go north."), owner_user_id=user_id
        )
    )

    assert response.reply == "A haunted reply"

    with session() as db:
        campaign = db.get_campaign(response.campaign_id)
        assert campaign is not None
        assert campaign.state is not None
        final_state = json.loads(campaign.state)
        assert final_state["npcs"]["old_caretaker"]["location"] == "entry_hall"

        events = db.list_campaign_events(response.campaign_id)
        event_types = [event.type for event in events]
        assert event_types == [
            "player_message_received",
            "action_parsed",
            "tool_executed",
            "game_state_updated",
            "world_action_failed",
            "narrator_response_created",
        ]
        failure_payload = json.loads(events[-2].payload_json or "{}")
        assert failure_payload["error_code"] == "destination_room_not_adjacent"


def test_director_provider_failure_rolls_back_transaction_and_allows_retry(
    monkeypatch,
) -> None:
    _enable_provider()

    client = TestClient(app)
    headers, user_id = _resolve_user(client, "director-provider-failure")
    idempotency_key = str(uuid4())
    headers["Idempotency-Key"] = idempotency_key

    monkeypatch.setattr(
        orchestrator_module.orchestrator.action_parser_agent,
        "parse",
        _fake_parse_move_north,
    )

    async def failing_propose(*, director_input, model=None):  # noqa: ANN001, ARG001, ANN202
        raise DirectorProviderError("Director model call failed.")

    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", failing_propose)

    failing_response = client.post(
        "/api/chat", json={"message": "I go north."}, headers=headers
    )

    assert failing_response.status_code == 502

    with session() as db:
        assert db.conn.execute(text("SELECT COUNT(*) FROM turns")).scalar_one() == 0
        assert db.conn.execute(text("SELECT COUNT(*) FROM game_events")).scalar_one() == 0
        assert db.conn.execute(text("SELECT COUNT(*) FROM campaigns")).scalar_one() == 0
        assert (
            db.conn.execute(text("SELECT COUNT(*) FROM model_requests")).scalar_one() == 0
        )
        idempotency_row = db.get_chat_request_idempotency(user_id, idempotency_key)
        assert idempotency_row is None or idempotency_row["status"] != "completed"

    monkeypatch.setattr(
        orchestrator_module.orchestrator.director_agent, "propose", _fake_no_action_propose
    )
    monkeypatch.setattr(
        orchestrator_module.orchestrator.narrator_agent,
        "generate",
        _fake_narrator_generate,
    )

    retry_response = client.post(
        "/api/chat", json={"message": "I go north."}, headers=headers
    )

    assert retry_response.status_code == 200
    assert retry_response.json()["reply"] == "A haunted reply"


async def _fake_no_action_propose(*, director_input, model=None):  # noqa: ANN001, ARG001, ANN202
    return _no_action_result()


def test_director_proposal_output_error_rolls_back_transaction(monkeypatch) -> None:
    _enable_provider()
    monkeypatch.setattr(
        orchestrator_module.orchestrator.action_parser_agent,
        "parse",
        _fake_parse_move_north,
    )

    async def failing_propose(*, director_input, model=None):  # noqa: ANN001, ARG001, ANN202
        raise DirectorProposalOutputError("Director returned invalid structured output.")

    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", failing_propose)

    client = TestClient(app)
    headers, user_id = _resolve_user(client, "director-schema-failure")

    response = client.post("/api/chat", json={"message": "I go north."}, headers=headers)

    assert response.status_code == 502
    with session() as db:
        assert db.conn.execute(text("SELECT COUNT(*) FROM campaigns")).scalar_one() == 0


def test_malformed_director_context_fails_explicitly_without_repair(monkeypatch) -> None:
    _enable_provider()

    client = TestClient(app)
    _headers, user_id = _resolve_user(client, "director-malformed-context")
    campaign_id = "campaign_malformed_director_context"

    malformed_state = build_fresh_campaign_state()
    malformed_state["clock"]["tick"] = "not-an-int"

    with session() as db:
        db.create_campaign(
            campaign_id=campaign_id,
            owner_user_id=user_id,
            name="Malformed Director Context",
            description="test fixture",
        )
        db.update_campaign_state(campaign_id, malformed_state)

    monkeypatch.setattr(
        orchestrator_module.orchestrator.action_parser_agent,
        "parse",
        _fake_parse_ambiguous,
    )

    def fail_if_called_propose(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("Director must not be called with an invalid context.")

    monkeypatch.setattr(
        orchestrator_module.orchestrator.director_agent, "propose", fail_if_called_propose
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            orchestrator_module.orchestrator.handle_chat(
                ChatRequest(message="hmm", campaign_id=campaign_id),
                owner_user_id=user_id,
            )
        )

    assert exc_info.value.status_code == 500

    with session() as db:
        campaign = db.get_campaign(campaign_id)
        assert campaign is not None
        assert campaign.state is not None
        assert json.loads(campaign.state)["clock"]["tick"] == "not-an-int"


def test_director_success_telemetry_records_estimated_tokens_and_usage(monkeypatch) -> None:
    _enable_provider()
    _install_move_and_narrator_stubs(monkeypatch)

    async def fake_propose(*, director_input, model=None):  # noqa: ANN001, ARG001, ANN202
        return DirectorAgentResult(
            proposal=NoActionProposal(),
            usage=ModelUsage(
                input_tokens=42,
                cached_input_tokens=2,
                cache_write_input_tokens=1,
                output_tokens=7,
                reasoning_output_tokens=3,
                total_tokens=49,
            ),
        )

    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", fake_propose)

    client = TestClient(app)
    _headers, user_id = _resolve_user(client, "director-telemetry")

    response = asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="I go north."), owner_user_id=user_id
        )
    )

    with session() as db:
        row = (
            db.conn.execute(
                text(
                    "SELECT * FROM model_requests WHERE campaign_id = :campaign_id "
                    "AND agent_name = 'Director'"
                ),
                {"campaign_id": response.campaign_id},
            )
            .mappings()
            .fetchone()
        )
        assert row is not None
        assert int(row["estimated_input_tokens"]) > 0
        assert int(row["actual_input_tokens"]) == 42
        assert int(row["cached_input_tokens"]) == 2
        assert int(row["cache_write_input_tokens"]) == 1
        assert int(row["actual_output_tokens"]) == 7
        assert int(row["reasoning_output_tokens"]) == 3
        assert int(row["actual_total_tokens"]) == 49
        assert bool(row["success"]) is True


def test_memory_maintenance_receives_final_post_director_state(monkeypatch) -> None:
    _enable_provider()
    _install_move_and_narrator_stubs(monkeypatch)

    from app.schemas.director import WorldActionProposal

    move_npc_action = MoveNpcWorldAction(
        npc_id="old_caretaker", destination_room_id="grand_corridor"
    )

    async def fake_propose(*, director_input, model=None):  # noqa: ANN001, ARG001, ANN202
        return DirectorAgentResult(
            proposal=WorldActionProposal(world_action=move_npc_action), usage=None
        )

    monkeypatch.setattr(orchestrator_module.orchestrator.director_agent, "propose", fake_propose)

    captured_state = {}
    original_maybe_store = MemoryService.maybe_store_semantic_memories

    def spy_maybe_store(self, **kwargs):  # noqa: ANN001, ANN003, ANN202
        captured_state["campaign_state"] = kwargs["campaign_state"]
        return original_maybe_store(self, **kwargs)

    monkeypatch.setattr(MemoryService, "maybe_store_semantic_memories", spy_maybe_store)

    client = TestClient(app)
    _headers, user_id = _resolve_user(client, "director-memory-ordering")

    asyncio.run(
        orchestrator_module.orchestrator.handle_chat(
            ChatRequest(message="I go north."), owner_user_id=user_id
        )
    )

    final_state = json.loads(captured_state["campaign_state"])
    assert final_state["npcs"]["old_caretaker"]["location"] == "grand_corridor"


def test_completed_keyed_replay_does_not_rerun_director_or_world_authority() -> None:
    _enable_provider()
    client = TestClient(app)
    headers, _user_id = _resolve_user(client, "director-idempotent-replay")
    headers["Idempotency-Key"] = str(uuid4())

    orchestrator_instance = orchestrator_module.orchestrator
    original_parse = orchestrator_instance.action_parser_agent.parse
    original_generate = orchestrator_instance.narrator_agent.generate
    original_propose = orchestrator_instance.director_agent.propose
    original_execute = orchestrator_instance.world_authority_executor.execute

    orchestrator_instance.action_parser_agent.parse = _fake_parse_move_north
    orchestrator_instance.narrator_agent.generate = _fake_narrator_generate
    orchestrator_instance.director_agent.propose = _fake_no_action_propose
    try:
        first = client.post(
            "/api/chat", json={"message": "I go north."}, headers=headers
        )
    finally:
        orchestrator_instance.action_parser_agent.parse = original_parse
        orchestrator_instance.narrator_agent.generate = original_generate
        orchestrator_instance.director_agent.propose = original_propose

    assert first.status_code == 200

    with session() as db:
        turns_before = db.conn.execute(text("SELECT COUNT(*) FROM turns")).scalar_one()
        events_before = db.conn.execute(text("SELECT COUNT(*) FROM game_events")).scalar_one()
        model_requests_before = db.conn.execute(
            text("SELECT COUNT(*) FROM model_requests")
        ).scalar_one()

    def fail_if_called_propose(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("Director must not run again on completed replay.")

    def fail_if_called_execute(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError(
            "WorldAuthorityExecutor must not run again on completed replay."
        )

    def fail_if_called_parse(**kwargs):  # noqa: ANN003, ANN202
        raise AssertionError("Action parser must not run again on completed replay.")

    def fail_if_called_generate(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("Narrator must not run again on completed replay.")

    orchestrator_instance.action_parser_agent.parse = fail_if_called_parse
    orchestrator_instance.narrator_agent.generate = fail_if_called_generate
    orchestrator_instance.director_agent.propose = fail_if_called_propose
    orchestrator_instance.world_authority_executor.execute = fail_if_called_execute
    try:
        second = client.post(
            "/api/chat", json={"message": "I go north."}, headers=headers
        )
    finally:
        orchestrator_instance.action_parser_agent.parse = original_parse
        orchestrator_instance.narrator_agent.generate = original_generate
        orchestrator_instance.director_agent.propose = original_propose
        orchestrator_instance.world_authority_executor.execute = original_execute

    assert second.status_code == 200
    assert second.json() == first.json()

    with session() as db:
        assert db.conn.execute(text("SELECT COUNT(*) FROM turns")).scalar_one() == turns_before
        assert (
            db.conn.execute(text("SELECT COUNT(*) FROM game_events")).scalar_one()
            == events_before
        )
        assert (
            db.conn.execute(text("SELECT COUNT(*) FROM model_requests")).scalar_one()
            == model_requests_before
        )
