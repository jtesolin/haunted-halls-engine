from __future__ import annotations

import asyncio
import json
import random
from typing import Any

import pytest

from app.db.session import session
from app.schemas.internal_auth import CANONICAL_GOOGLE_ISSUER
from app.agents.action_parser import ActionParserAgent
from app.schemas.chat import ActionType, ParsedAction
from app.services.tool_executor import ToolExecutor
from app.tools.registry import ToolRegistry


class ParityMCPClient:
    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        args = arguments.get("args", [])
        state = args[0]

        if name == "create_npc":
            npc_id = args[1]
            room_id = args[2]
            state.setdefault("npcs", {})[npc_id] = {"room": room_id}
            return {"return": None}

        if name == "advance_time":
            ticks = int(args[1])
            clock = state.setdefault("clock", {})
            clock["tick"] = int(clock.get("tick", 0)) + ticks
            return {"return": None}

        if name == "search_lore":
            fact = args[1]
            state.setdefault("facts", []).append(fact)
            return {"return": None}

        raise RuntimeError(f"unknown mcp tool: {name}")


class FailingMCPClient:
    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:  # noqa: ARG002
        raise RuntimeError(f"cannot reach mcp tool {name}")


class RecordingMCPClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        return {"return": {"success": False}}


class MaliciousItemMCPClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        state = arguments["args"][0]
        state.setdefault("player", {}).setdefault("inventory", []).append("magic_sword")
        return {"return": {"state": state}}


def _build_local_executor() -> ToolExecutor:
    registry = ToolRegistry(mode="local")
    executor = ToolExecutor(registry=registry)
    registry.register("move_player", executor.move_player)
    registry.register("take_item", executor.take_item)
    registry.register("drop_item", executor.drop_item)
    registry.register("spawn_npc", executor.spawn_npc)
    registry.register("advance_clock", executor.advance_clock)
    registry.register("record_fact", executor.record_fact)
    return executor


def _build_hybrid_executor() -> ToolExecutor:
    registry = ToolRegistry(mode="hybrid", mcp_client=ParityMCPClient())
    executor = ToolExecutor(registry=registry)
    registry.register("move_player", executor.move_player)
    registry.register("take_item", executor.take_item)
    registry.register("drop_item", executor.drop_item)
    registry.register("spawn_npc", executor.spawn_npc)
    registry.register("advance_clock", executor.advance_clock)
    registry.register("record_fact", executor.record_fact)
    registry.register_mcp("spawn_npc", "create_npc")
    registry.register_mcp("advance_clock", "advance_time")
    registry.register_mcp("record_fact", "search_lore")
    return executor


def test_tool_executor_transport_parity_for_non_item_tools() -> None:
    local_executor = _build_local_executor()
    hybrid_executor = _build_hybrid_executor()

    parsed_action = ParsedAction(
        raw_text="wait",
        action=ActionType.WAIT,
        parameters={"amount": 2},
        parse_status="ok",
    )

    # Seed identically so both executors roll the same random starting inventory.
    random.seed(1234)
    local_state, local_result = local_executor.execute(parsed_action=parsed_action, campaign_state="No campaign state yet.")
    random.seed(1234)
    hybrid_state, hybrid_result = hybrid_executor.execute(parsed_action=parsed_action, campaign_state="No campaign state yet.")

    assert local_result.success == hybrid_result.success
    assert local_result.applied_tools == hybrid_result.applied_tools
    assert local_result.summary == hybrid_result.summary
    assert local_state == hybrid_state


def test_tool_executor_returns_structured_dispatch_errors() -> None:
    registry = ToolRegistry(mode="mcp", mcp_client=FailingMCPClient())
    registry.register_mcp("advance_clock", "advance_time")
    executor = ToolExecutor(registry=registry)

    parsed_action = ParsedAction(
        raw_text="wait",
        action=ActionType.WAIT,
        parameters={"amount": 1},
        parse_status="ok",
    )

    _, result = executor.execute(parsed_action=parsed_action, campaign_state="No campaign state yet.")

    assert result.success is False
    assert result.summary == "Tool dispatch failed for advance_clock."
    assert result.errors[0] == "tool_dispatch_failed"
    assert result.errors[1] == "tool:advance_clock"
    assert result.errors[2].startswith("reason:MCP call failed for tool: advance_time")


def test_take_item_success_transfers_authoritative_item() -> None:
    executor = _build_local_executor()

    state, result = executor.execute(
        parsed_action=ParsedAction(
            raw_text="take brass key",
            action=ActionType.TAKE,
            target="brass_key",
            parse_status="ok",
        ),
        campaign_state='{"player": {"location": "library", "inventory": []}}',
    )

    assert result.success is True
    assert result.item_id == "brass_key"
    assert result.moved_from == "room:library"
    assert result.moved_to == "player:current"
    assert state["items"]["brass_key"]["location"] == "player:current"
    assert "brass_key" in state["player"]["inventory"]


def test_take_item_named_resolution_succeeds() -> None:
    executor = _build_local_executor()

    state, result = executor.execute(
        parsed_action=ParsedAction(
            raw_text="pick up the brass key",
            action=ActionType.TAKE,
            target="brass key",
            parse_status="ok",
        ),
        campaign_state='{"player": {"location": "library", "inventory": []}}',
    )

    assert result.success is True
    assert result.item_id == "brass_key"
    assert state["items"]["brass_key"]["location"] == "player:current"


def test_take_item_not_in_room_fails_without_mutation() -> None:
    executor = _build_local_executor()

    campaign_state = '{"player": {"location": "entry_hall", "inventory": []}}'
    state, result = executor.execute(
        parsed_action=ParsedAction(
            raw_text="take brass key",
            action=ActionType.TAKE,
            target="brass_key",
            parse_status="ok",
        ),
        campaign_state=campaign_state,
    )

    assert result.success is False
    assert result.error_code == "item_not_in_room"
    assert state["items"]["brass_key"]["location"] == "room:library"
    assert "brass_key" not in state["player"]["inventory"]


def test_take_non_portable_item_fails_without_mutation() -> None:
    executor = _build_local_executor()

    state, result = executor.execute(
        parsed_action=ParsedAction(
            raw_text="take heavy statue",
            action=ActionType.TAKE,
            target="heavy_statue",
            parse_status="ok",
        ),
        campaign_state='{"player": {"location": "entry_hall", "inventory": []}}',
    )

    assert result.success is False
    assert result.error_code == "item_not_portable"
    assert state["items"]["heavy_statue"]["location"] == "room:entry_hall"
    assert state["player"]["inventory"] == []


def test_take_unknown_item_does_not_create_item() -> None:
    executor = _build_local_executor()

    state, result = executor.execute(
        parsed_action=ParsedAction(
            raw_text="take moon sword",
            action=ActionType.TAKE,
            target="moon_sword",
            parse_status="ok",
        ),
        campaign_state='{"player": {"location": "library", "inventory": []}}',
    )

    assert result.success is False
    assert result.error_code == "item_not_found"
    assert "moon_sword" not in state["items"]
    assert "moon_sword" not in state["player"]["inventory"]


def test_drop_item_success_transfers_back_to_room() -> None:
    executor = _build_local_executor()

    campaign_state = json.dumps(
        {
            "player": {"location": "library", "inventory": ["brass_key"]},
            "items": {
                "brass_key": {
                    "id": "brass_key",
                    "name": "Brass Key",
                    "description": "A key",
                    "location": "player:current",
                    "portable": True,
                    "quantity": 1,
                    "tags": ["key"],
                    "aliases": ["brass key"],
                    "properties": {},
                }
            },
        }
    )

    state, result = executor.execute(
        parsed_action=ParsedAction(
            raw_text="drop brass key",
            action=ActionType.DROP,
            target="brass_key",
            parse_status="ok",
        ),
        campaign_state=campaign_state,
    )

    assert result.success is True
    assert result.item_id == "brass_key"
    assert result.moved_from == "player:current"
    assert result.moved_to == "room:library"
    assert state["items"]["brass_key"]["location"] == "room:library"
    assert "brass_key" not in state["player"]["inventory"]


def test_drop_item_not_owned_fails_without_mutation() -> None:
    executor = _build_local_executor()

    state, result = executor.execute(
        parsed_action=ParsedAction(
            raw_text="drop brass key",
            action=ActionType.DROP,
            target="brass_key",
            parse_status="ok",
        ),
        campaign_state='{"player": {"location": "library", "inventory": []}}',
    )

    assert result.success is False
    assert result.error_code == "item_not_in_inventory"
    assert state["items"]["brass_key"]["location"] == "room:library"


def test_take_ambiguous_item_fails_deterministically() -> None:
    executor = _build_local_executor()

    campaign_state = json.dumps(
        {
            "player": {"location": "library", "inventory": []},
            "items": {
                "old_book": {
                    "id": "old_book",
                    "name": "Old Book",
                    "description": "A",
                    "location": "room:library",
                    "portable": True,
                    "quantity": 1,
                    "tags": ["book"],
                    "aliases": ["book"],
                    "properties": {},
                },
                "ledger": {
                    "id": "ledger",
                    "name": "Dusty Ledger",
                    "description": "B",
                    "location": "room:library",
                    "portable": True,
                    "quantity": 1,
                    "tags": ["book"],
                    "aliases": ["book"],
                    "properties": {},
                },
            },
        }
    )

    state, result = executor.execute(
        parsed_action=ParsedAction(
            raw_text="take book",
            action=ActionType.TAKE,
            target="book",
            parse_status="ok",
        ),
        campaign_state=campaign_state,
    )

    assert result.success is False
    assert result.error_code == "ambiguous_item"
    assert state["items"]["old_book"]["location"] == "room:library"
    assert state["items"]["ledger"]["location"] == "room:library"


def test_take_item_not_ambiguous_when_tag_match_is_elsewhere() -> None:
    executor = _build_local_executor()

    campaign_state = json.dumps(
        {
            "player": {"location": "library", "inventory": ["worn_journal"]},
            "items": {
                "old_book": {
                    "id": "old_book",
                    "name": "Old Book",
                    "description": "A mold-speckled book.",
                    "location": "room:library",
                    "portable": True,
                    "quantity": 1,
                    "tags": ["book"],
                    "aliases": ["old book", "moldy book"],
                    "properties": {},
                },
                "worn_journal": {
                    "id": "worn_journal",
                    "name": "Worn Journal",
                    "description": "A traveler's journal.",
                    "location": "player:current",
                    "portable": True,
                    "quantity": 1,
                    "tags": ["book"],
                    "aliases": ["journal", "worn journal"],
                    "properties": {},
                },
            },
        }
    )

    state, result = executor.execute(
        parsed_action=ParsedAction(
            raw_text="take book",
            action=ActionType.TAKE,
            target="book",
            parse_status="ok",
        ),
        campaign_state=campaign_state,
    )

    assert result.success is True
    assert result.item_id == "old_book"
    assert state["items"]["old_book"]["location"] == "player:current"
    assert state["items"]["worn_journal"]["location"] == "player:current"


def test_drop_item_not_ambiguous_when_tag_match_is_elsewhere() -> None:
    executor = _build_local_executor()

    campaign_state = json.dumps(
        {
            "player": {"location": "library", "inventory": ["worn_journal"]},
            "items": {
                "old_book": {
                    "id": "old_book",
                    "name": "Old Book",
                    "description": "A mold-speckled book.",
                    "location": "room:library",
                    "portable": True,
                    "quantity": 1,
                    "tags": ["book"],
                    "aliases": ["old book", "moldy book"],
                    "properties": {},
                },
                "worn_journal": {
                    "id": "worn_journal",
                    "name": "Worn Journal",
                    "description": "A traveler's journal.",
                    "location": "player:current",
                    "portable": True,
                    "quantity": 1,
                    "tags": ["book"],
                    "aliases": ["journal", "worn journal"],
                    "properties": {},
                },
            },
        }
    )

    state, result = executor.execute(
        parsed_action=ParsedAction(
            raw_text="drop book",
            action=ActionType.DROP,
            target="book",
            parse_status="ok",
        ),
        campaign_state=campaign_state,
    )

    assert result.success is True
    assert result.item_id == "worn_journal"
    assert state["items"]["worn_journal"]["location"] == "room:library"
    assert state["items"]["old_book"]["location"] == "room:library"


def test_observe_action_succeeds_without_mutation() -> None:
    executor = _build_local_executor()

    campaign_state = json.dumps(
        {
            "player": {"location": "library", "inventory": ["brass_key"]},
            "items": {
                "brass_key": {
                    "id": "brass_key",
                    "name": "Brass Key",
                    "description": "A key",
                    "location": "player:current",
                    "portable": True,
                    "quantity": 1,
                    "tags": ["key"],
                    "aliases": ["brass key"],
                    "properties": {},
                },
                "old_book": {
                    "id": "old_book",
                    "name": "Old Book",
                    "description": "A mold-speckled book.",
                    "location": "room:library",
                    "portable": True,
                    "quantity": 1,
                    "tags": ["book"],
                    "aliases": ["old book"],
                    "properties": {},
                },
            },
        }
    )

    state, result = executor.execute(
        parsed_action=ParsedAction(
            raw_text="look around",
            action=ActionType.OBSERVE,
            parse_status="ok",
        ),
        campaign_state=campaign_state,
    )

    assert result.success is True
    assert result.current_location == "library"
    assert result.current_room_name is not None
    assert {item["id"] for item in result.available_items} == {"old_book"}
    assert result.inventory_items == ["brass_key"]
    assert result.state_delta == {}
    assert state["items"]["brass_key"]["location"] == "player:current"
    assert state["items"]["old_book"]["location"] == "room:library"


@pytest.mark.parametrize(
    "parsed_action,campaign_state",
    [
        (
            ParsedAction(raw_text="go north", action=ActionType.MOVE, target="north", parse_status="ok"),
            '{"player": {"location": "entry_hall", "inventory": []}}',
        ),
        (
            ParsedAction(raw_text="wait", action=ActionType.WAIT, parameters={"amount": 2}, parse_status="ok"),
            "No campaign state yet.",
        ),
    ],
)
def test_world_and_clock_paths_still_work(parsed_action: ParsedAction, campaign_state: str) -> None:
    executor = _build_local_executor()
    state, result = executor.execute(parsed_action=parsed_action, campaign_state=campaign_state)

    assert isinstance(state, dict)
    assert result.summary


def test_no_duplicate_item_ownership_after_take_and_drop() -> None:
    executor = _build_local_executor()

    state, take_result = executor.execute(
        parsed_action=ParsedAction(
            raw_text="take brass key",
            action=ActionType.TAKE,
            target="brass_key",
            parse_status="ok",
        ),
        campaign_state='{"player": {"location": "library", "inventory": []}}',
    )
    assert take_result.success is True

    player_has_item = "brass_key" in state["player"]["inventory"]
    room_has_item = any(
        item_id == "brass_key" and item.get("location") == "room:library"
        for item_id, item in state["items"].items()
    )
    assert player_has_item is True
    assert room_has_item is False

    dropped_state, drop_result = executor.execute(
        parsed_action=ParsedAction(
            raw_text="drop brass key",
            action=ActionType.DROP,
            target="brass_key",
            parse_status="ok",
        ),
        campaign_state=json.dumps(state),
    )
    assert drop_result.success is True

    player_has_item_after = "brass_key" in dropped_state["player"]["inventory"]
    room_has_item_after = dropped_state["items"]["brass_key"]["location"] == "room:library"
    assert player_has_item_after is False
    assert room_has_item_after is True


def test_item_state_persists_across_save_and_reload_round_trip() -> None:
    executor = _build_local_executor()

    state_after_take, take_result = executor.execute(
        parsed_action=ParsedAction(
            raw_text="take brass key",
            action=ActionType.TAKE,
            target="brass_key",
            parse_status="ok",
        ),
        campaign_state='{"player": {"location": "library", "inventory": []}}',
    )
    assert take_result.success is True

    with session() as db:
        owner_user_id = db.resolve_internal_user(
            identity_provider="google",
            provider_issuer=CANONICAL_GOOGLE_ISSUER,
            provider_subject="phase6b-owner",
            email="phase6b-owner@example.com",
            email_verified=True,
            display_name="Phase 6B Owner",
            avatar_url="https://example.com/avatar.png",
        ).id
        campaign_id = "campaign_phase6b"
        db.create_campaign(campaign_id, owner_user_id, "Phase 6B", "Item model test")
        db.update_campaign_state(campaign_id, state_after_take)
        persisted = db.get_campaign(campaign_id)
        assert persisted is not None
        assert persisted.state is not None

        loaded_state = json.loads(persisted.state)
        assert loaded_state["items"]["brass_key"]["location"] == "player:current"
        assert "brass_key" in loaded_state["player"]["inventory"]

    state_after_drop, drop_result = executor.execute(
        parsed_action=ParsedAction(
            raw_text="drop brass key",
            action=ActionType.DROP,
            target="brass_key",
            parse_status="ok",
        ),
        campaign_state=json.dumps(loaded_state),
    )
    assert drop_result.success is True

    with session() as db:
        db.update_campaign_state("campaign_phase6b", state_after_drop)
        persisted_again = db.get_campaign("campaign_phase6b")
        assert persisted_again is not None
        assert persisted_again.state is not None
        reloaded = json.loads(persisted_again.state)
        assert reloaded["items"]["brass_key"]["location"] == "room:library"
        assert "brass_key" not in reloaded["player"]["inventory"]


def test_take_drop_cannot_be_bypassed_by_mcp_mappings() -> None:
    malicious_client = MaliciousItemMCPClient()
    registry = ToolRegistry(mode="hybrid", mcp_client=malicious_client)
    executor = ToolExecutor(registry=registry)

    # Intentionally map item tools to MCP names; TAKE/DROP should still run local deterministic logic.
    registry.register("take_item", executor.take_item)
    registry.register("drop_item", executor.drop_item)
    registry.register_mcp("take_item", "inject_item")
    registry.register_mcp("drop_item", "inject_item")

    state, result = executor.execute(
        parsed_action=ParsedAction(
            raw_text="take moon sword",
            action=ActionType.TAKE,
            target="moon_sword",
            parse_status="ok",
        ),
        campaign_state='{"player": {"location": "library", "inventory": []}}',
    )

    assert result.success is False
    assert result.error_code == "item_not_found"
    assert malicious_client.calls == []
    assert "magic_sword" not in state["player"]["inventory"]


def test_parser_freeform_take_integrates_with_deterministic_item_transfer() -> None:
    parser = ActionParserAgent()
    executor = _build_local_executor()

    parsed_action = asyncio.run(
        parser.parse(
            message="I pick up the tarnished brass key.",
            campaign_state='{"player": {"location": "library", "inventory": []}}',
            recent_turns=[],
            memory_context=[],
            deterministic_only=True,
        )
    )

    assert parsed_action.action == ActionType.TAKE
    assert parsed_action.target == "tarnished brass key"

    state, result = executor.execute(
        parsed_action=parsed_action,
        campaign_state='{"player": {"location": "library", "inventory": []}}',
    )

    assert result.success is True
    assert result.item_id == "brass_key"
    assert state["items"]["brass_key"]["location"] == "player:current"


def test_movement_rejects_non_adjacent_room_without_mcp_bypass() -> None:
    client = RecordingMCPClient()
    registry = ToolRegistry(mode="hybrid", mcp_client=client)
    executor = ToolExecutor(registry=registry)
    registry.register("move_player", executor.move_player)

    parsed_action = ParsedAction(
        raw_text="go to the crypt",
        action=ActionType.MOVE,
        target="crypt",
        parse_status="ok",
    )
    state, result = executor.execute(
        parsed_action=parsed_action,
        campaign_state='{"player": {"location": "entry_hall", "inventory": []}}',
    )

    assert result.success is False
    assert result.error_code == "invalid_exit"
    assert state["player"]["location"] == "entry_hall"
    assert client.calls == []
