from __future__ import annotations

import asyncio
import json
import random
from typing import Any

import pytest

from app.db.session import session
from app.game.items import ensure_items_state
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


def test_talk_to_nearby_npc_success_and_non_mutating() -> None:
    executor = _build_local_executor()
    campaign_state = json.dumps({
        "player": {"location": "entry_hall", "inventory": []},
        "npcs": {
            "old_caretaker": {
                "id": "old_caretaker",
                "name": "Old Caretaker",
                "location": "entry_hall",
                "status": "active",
                "disposition": "neutral",
                "aliases": ["caretaker"],
                "tags": ["human"],
            }
        },
    })

    state, result = executor.execute(
        parsed_action=ParsedAction(
            raw_text="talk to old caretaker",
            action=ActionType.TALK,
            target="old caretaker",
            parse_status="ok",
        ),
        campaign_state=campaign_state,
    )

    assert result.success is True
    assert result.applied_tools == ["talk_to_npc"]
    assert result.npc_id == "old_caretaker"
    assert result.npc_name == "Old Caretaker"
    assert result.state_delta == {}
    assert state["npcs"]["old_caretaker"]["status"] == "active"
    assert state["npcs"]["old_caretaker"]["disposition"] == "neutral"


def test_talk_resolves_by_id_name_alias() -> None:
    executor = _build_local_executor()
    campaign_state = json.dumps({
        "player": {"location": "entry_hall", "inventory": []},
        "npcs": {
            "old_caretaker": {
                "id": "old_caretaker",
                "name": "Old Caretaker",
                "location": "entry_hall",
                "status": "active",
                "disposition": "neutral",
                "aliases": ["caretaker"],
                "tags": ["human"],
            }
        },
    })

    for target in ["old_caretaker", "Old Caretaker", "caretaker"]:
        _, result = executor.execute(
            parsed_action=ParsedAction(
                raw_text=f"talk to {target}",
                action=ActionType.TALK,
                target=target,
                parse_status="ok",
            ),
            campaign_state=campaign_state,
        )
        assert result.success is True
        assert result.npc_id == "old_caretaker"


def test_talk_to_off_room_npc_fails_with_not_present() -> None:
    executor = _build_local_executor()
    state, result = executor.execute(
        parsed_action=ParsedAction(
            raw_text="talk to library ghost",
            action=ActionType.TALK,
            target="library ghost",
            parse_status="ok",
        ),
        campaign_state=json.dumps({
            "player": {"location": "entry_hall", "inventory": []},
            "npcs": {
                "library_ghost": {
                    "id": "library_ghost",
                    "name": "Library Ghost",
                    "location": "library",
                    "status": "active",
                    "disposition": "neutral",
                    "aliases": ["ghost"],
                    "tags": ["undead"],
                }
            },
        }),
    )

    assert result.success is False
    assert result.error_code == "npc_not_present"
    assert state["npcs"]["library_ghost"]["location"] == "library"


def test_talk_to_absent_npc_fails_with_not_present() -> None:
    executor = _build_local_executor()
    state, result = executor.execute(
        parsed_action=ParsedAction(
            raw_text="talk to old caretaker",
            action=ActionType.TALK,
            target="old caretaker",
            parse_status="ok",
        ),
        campaign_state=json.dumps({
            "player": {"location": "entry_hall", "inventory": []},
            "npcs": {
                "old_caretaker": {
                    "id": "old_caretaker",
                    "name": "Old Caretaker",
                    "location": "entry_hall",
                    "status": "absent",
                    "disposition": "neutral",
                    "aliases": ["caretaker"],
                    "tags": ["human"],
                }
            },
        }),
    )

    assert result.success is False
    assert result.error_code == "npc_not_present"
    assert state["npcs"]["old_caretaker"]["status"] == "absent"


def test_talk_ambiguous_nearby_npc_fails_without_mutation() -> None:
    executor = _build_local_executor()
    state, result = executor.execute(
        parsed_action=ParsedAction(
            raw_text="talk to caretaker",
            action=ActionType.TALK,
            target="caretaker",
            parse_status="ok",
        ),
        campaign_state=json.dumps({
            "player": {"location": "entry_hall", "inventory": []},
            "npcs": {
                "old_caretaker": {
                    "id": "old_caretaker",
                    "name": "Old Caretaker",
                    "location": "entry_hall",
                    "status": "active",
                    "disposition": "neutral",
                    "aliases": ["caretaker"],
                    "tags": ["human"],
                },
                "dormitory_guard": {
                    "id": "dormitory_guard",
                    "name": "Dormitory Guard",
                    "location": "entry_hall",
                    "status": "active",
                    "disposition": "neutral",
                    "aliases": ["caretaker"],
                    "tags": ["human"],
                },
            },
        }),
    )

    assert result.success is False
    assert result.error_code == "ambiguous_npc"
    assert state["npcs"]["old_caretaker"]["status"] == "active"


def test_talk_unknown_npc_fails_without_mutation() -> None:
    executor = _build_local_executor()
    state, result = executor.execute(
        parsed_action=ParsedAction(
            raw_text="talk to unknown ghost",
            action=ActionType.TALK,
            target="unknown ghost",
            parse_status="ok",
        ),
        campaign_state=json.dumps({
            "player": {"location": "entry_hall", "inventory": []},
            "npcs": {"old_caretaker": {"id": "old_caretaker", "name": "Old Caretaker", "location": "entry_hall", "status": "active"}},
        }),
    )

    assert result.success is False
    assert result.error_code == "npc_not_found"
    assert state["npcs"]["old_caretaker"]["status"] == "active"


def test_talk_invalid_current_location_fails() -> None:
    executor = _build_local_executor()
    _, result = executor.execute(
        parsed_action=ParsedAction(
            raw_text="talk to old caretaker",
            action=ActionType.TALK,
            target="old caretaker",
            parse_status="ok",
        ),
        campaign_state=json.dumps({"player": {"location": "nowhere", "inventory": []}}),
    )

    assert result.success is False
    assert result.error_code == "invalid_current_location"


def test_unsupported_attack_and_unknown_item_interactions_are_explicit() -> None:
    executor = _build_local_executor()
    base_state = json.dumps({
        "player": {"location": "entry_hall", "inventory": []},
        "npcs": {"old_caretaker": {"id": "old_caretaker", "name": "Old Caretaker", "location": "entry_hall", "status": "active"}},
    })

    for action, target, expected_code in [
        (ActionType.ATTACK, "caretaker", "combat_not_supported"),
        (ActionType.USE, "door", "item_not_found"),
        (ActionType.INTERACT, "door", "item_not_found"),
    ]:
        _, result = executor.execute(
            parsed_action=ParsedAction(
                raw_text=f"{action.value} {target}",
                action=action,
                target=target,
                parse_status="ok",
            ),
            campaign_state=base_state,
        )
        assert result.success is False
        assert result.error_code == expected_code
        assert result.state_delta == {}


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


def test_item_normalization_merges_canonical_properties_without_backfilling_starters() -> None:
    state = {
        "items": {
            "old_book": {"location": "room:library", "properties": {"is_open": True}},
            "box_of_matches": {"location": "player:current", "properties": {}},
        }
    }

    items = ensure_items_state(state)

    assert items["old_book"]["properties"] == {"openable": True, "is_open": True}
    assert items["box_of_matches"]["properties"] == {"ignition_source": True}
    assert "tinderbox" not in items


@pytest.mark.parametrize(
    ("action", "mode", "campaign_state", "property_name", "expected_value"),
    [
        (ActionType.INTERACT, "open", '{"player": {"location": "library", "inventory": []}}', "is_open", True),
        (
            ActionType.INTERACT,
            "open",
            '{"player": {"location": "entry_hall", "inventory": ["old_book"]}, "items": {"old_book": {"location": "player:current", "properties": {}}}}',
            "is_open",
            True,
        ),
        (
            ActionType.INTERACT,
            "close",
            '{"player": {"location": "library", "inventory": []}, "items": {"old_book": {"location": "room:library", "properties": {"is_open": true}}}}',
            "is_open",
            False,
        ),
        (
            ActionType.INTERACT,
            "extinguish",
            '{"player": {"location": "dining_room", "inventory": []}, "items": {"candle": {"location": "room:dining_room", "properties": {"lit": true}}}}',
            "lit",
            False,
        ),
    ],
)
def test_interact_item_mutates_accessible_item(
    action: ActionType,
    mode: str,
    campaign_state: str,
    property_name: str,
    expected_value: bool,
) -> None:
    state, result = _build_local_executor().execute(
        parsed_action=ParsedAction(raw_text=f"{mode} item", action=action, target="old book" if property_name == "is_open" else "candle", parameters={"interaction_mode": mode}, parse_status="ok"),
        campaign_state=campaign_state,
    )

    assert result.success is True
    assert result.applied_tools == ["interact_item"]
    assert result.interaction_mode == mode
    assert result.state_delta["items"][result.item_id]["properties"][property_name]["to"] is expected_value
    assert state["items"][result.item_id]["properties"][property_name] is expected_value


@pytest.mark.parametrize(
    ("action", "target", "parameters", "campaign_state", "error_code", "expected_location"),
    [
        (ActionType.INTERACT, "old book", {"interaction_mode": "open"}, '{"player": {"location": "library"}, "items": {"old_book": {"location": "room:library", "properties": {"is_open": true}}}}', "item_already_open", "library"),
        (ActionType.INTERACT, "old book", {"interaction_mode": "close"}, '{"player": {"location": "library"}}', "item_already_closed", "library"),
        (ActionType.INTERACT, "heavy statue", {"interaction_mode": "open"}, '{"player": {"location": "entry_hall"}}', "unsupported_interaction", "entry_hall"),
        (ActionType.INTERACT, "candle", {"interaction_mode": "extinguish"}, '{"player": {"location": "dining_room"}}', "item_already_extinguished", "dining_room"),
        (ActionType.INTERACT, "old book", {"interaction_mode": "open"}, '{"player": {"location": "entry_hall"}}', "item_not_accessible", "entry_hall"),
        (ActionType.INTERACT, "unknown", {"interaction_mode": "open"}, '{"player": {"location": "library"}}', "item_not_found", "library"),
        (ActionType.INTERACT, "old book", {"interaction_mode": "open"}, '{"player": {"location": "nowhere"}}', "invalid_current_location", "nowhere"),
    ],
)
def test_interact_item_rejections_do_not_mutate(
    action: ActionType, target: str, parameters: dict[str, str], campaign_state: str, error_code: str, expected_location: str
) -> None:
    state, result = _build_local_executor().execute(
        parsed_action=ParsedAction(raw_text="interact", action=action, target=target, parameters=parameters, parse_status="ok"),
        campaign_state=campaign_state,
    )

    assert result.success is False
    assert result.error_code == error_code
    assert result.state_delta == {}
    assert state["player"]["location"] == expected_location


def test_interact_item_rejects_ambiguous_accessible_target() -> None:
    state, result = _build_local_executor().execute(
        parsed_action=ParsedAction(raw_text="open book", action=ActionType.INTERACT, target="book", parameters={"interaction_mode": "open"}, parse_status="ok"),
        campaign_state='{"player": {"location": "library"}, "items": {"old_book": {"location": "room:library", "tags": ["book"], "properties": {}}, "worn_journal": {"location": "player:current", "tags": ["book"], "properties": {}}}}',
    )

    assert result.success is False
    assert result.error_code == "ambiguous_item"
    assert state["items"]["old_book"]["properties"]["is_open"] is False


@pytest.mark.parametrize("tool_name", ["matches", "tinderbox"])
def test_use_ignition_source_lights_candle_and_infers_light(tool_name: str) -> None:
    state, result = _build_local_executor().execute(
        parsed_action=ParsedAction(raw_text=f"use {tool_name} on candle", action=ActionType.USE, target="candle", parameters={"with_item": tool_name}, parse_status="ok"),
        campaign_state=json.dumps({"player": {"location": "dining_room", "inventory": [tool_name]}, "items": {"box_of_matches" if tool_name == "matches" else "tinderbox": {"location": "player:current", "properties": {}}, "candle": {"location": "room:dining_room", "properties": {}}}}),
    )

    assert result.success is True
    assert result.interaction_mode == "light"
    assert result.with_item_id in {"box_of_matches", "tinderbox"}
    assert state["items"]["candle"]["properties"]["lit"] is True


def test_use_non_light_combination_rejects_as_unsupported_interaction() -> None:
    _, result = _build_local_executor().execute(
        parsed_action=ParsedAction(raw_text="use brass key on old book", action=ActionType.USE, target="old book", parameters={"with_item": "brass key"}, parse_status="ok"),
        campaign_state='{"player": {"location": "library", "inventory": ["brass_key"]}, "items": {"brass_key": {"location": "player:current", "properties": {}}, "old_book": {"location": "room:library", "properties": {}}}}',
    )

    assert result.success is False
    assert result.error_code == "unsupported_interaction"
    assert result.state_delta == {}


def test_use_light_on_non_lightable_target_rejects_as_unsupported_interaction() -> None:
    _, result = _build_local_executor().execute(
        parsed_action=ParsedAction(raw_text="light old book with brass key", action=ActionType.USE, target="old book", parameters={"interaction_mode": "light", "with_item": "brass key"}, parse_status="ok"),
        campaign_state='{"player": {"location": "library", "inventory": ["brass_key"]}, "items": {"brass_key": {"location": "player:current", "properties": {}}, "old_book": {"location": "room:library", "properties": {}}}}',
    )

    assert result.success is False
    assert result.error_code == "unsupported_interaction"
    assert result.state_delta == {}


@pytest.mark.parametrize(
    ("parameters", "campaign_state", "error_code"),
    [
        ({"interaction_mode": "light", "with_item": "matches"}, '{"player": {"location": "dining_room"}}', "with_item_not_found"),
        ({"interaction_mode": "light", "with_item": "matches"}, '{"player": {"location": "dining_room"}, "items": {"box_of_matches": {"location": "room:library", "properties": {}}}}', "with_item_not_in_inventory"),
        ({"interaction_mode": "light", "with_item": "brass key"}, '{"player": {"location": "dining_room", "inventory": ["brass_key"]}, "items": {"brass_key": {"location": "player:current", "properties": {}}}}', "required_item_missing"),
        ({"interaction_mode": "light", "with_item": "matches"}, '{"player": {"location": "dining_room", "inventory": ["box_of_matches"]}, "items": {"box_of_matches": {"location": "player:current", "properties": {}}, "candle": {"location": "room:dining_room", "properties": {"lit": true}}}}', "item_already_lit"),
    ],
)
def test_use_light_rejections_do_not_mutate(parameters: dict[str, str], campaign_state: str, error_code: str) -> None:
    _, result = _build_local_executor().execute(
        parsed_action=ParsedAction(raw_text="light candle", action=ActionType.USE, target="candle", parameters=parameters, parse_status="ok"),
        campaign_state=campaign_state,
    )

    assert result.success is False
    assert result.error_code == error_code
    assert result.state_delta == {}


def test_use_rejects_ambiguous_inventory_tool_without_mcp_dispatch() -> None:
    client = RecordingMCPClient()
    registry = ToolRegistry(mode="hybrid", mcp_client=client)
    executor = ToolExecutor(registry=registry)
    state, result = executor.execute(
        parsed_action=ParsedAction(raw_text="use matches on candle", action=ActionType.USE, target="candle", parameters={"with_item": "matches"}, parse_status="ok"),
        campaign_state='{"player": {"location": "dining_room"}, "items": {"box_of_matches": {"location": "player:current", "aliases": ["matches"], "properties": {}}, "spare_matches": {"location": "player:current", "aliases": ["matches"], "properties": {}}, "candle": {"location": "room:dining_room", "properties": {}}}}',
    )

    assert result.success is False
    assert result.error_code == "ambiguous_with_item"
    assert state["items"]["candle"]["properties"]["lit"] is False
    assert client.calls == []


def test_interaction_state_delta_survives_database_round_trip() -> None:
    state, result = _build_local_executor().execute(
        parsed_action=ParsedAction(raw_text="open old book", action=ActionType.INTERACT, target="old book", parameters={"interaction_mode": "open"}, parse_status="ok"),
        campaign_state='{"player": {"location": "library", "inventory": []}}',
    )
    assert result.state_delta

    with session() as db:
        owner_id = db.resolve_internal_user(identity_provider="google", provider_issuer=CANONICAL_GOOGLE_ISSUER, provider_subject="phase6d-owner", email="phase6d-owner@example.com", email_verified=True, display_name="Phase 6D Owner", avatar_url="https://example.com/avatar.png").id
        db.create_campaign("campaign_phase6d", owner_id, "Phase 6D", "Interaction test")
        db.update_campaign_state("campaign_phase6d", state)
        persisted = db.get_campaign("campaign_phase6d")

    assert persisted is not None and persisted.state is not None
    assert json.loads(persisted.state)["items"]["old_book"]["properties"]["is_open"] is True
