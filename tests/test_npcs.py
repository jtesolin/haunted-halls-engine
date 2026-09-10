from __future__ import annotations

import json

from app.agents.action_parser import ActionParserAgent
from app.game.npcs import (
    default_npcs_state,
    ensure_npcs_state,
    nearby_npc_ids_for_room,
    nearby_npcs_for_room,
    resolve_npc_ids,
)
from app.schemas.chat import ActionType, ParsedAction
from app.services.tool_executor import ToolExecutor
from app.tools.registry import ToolRegistry


def _executor() -> ToolExecutor:
    registry = ToolRegistry(mode="local")
    executor = ToolExecutor(registry=registry)
    registry.register("move_player", executor.move_player)
    registry.register("take_item", executor.take_item)
    registry.register("drop_item", executor.drop_item)
    registry.register("advance_clock", executor.advance_clock)
    return executor


def test_development_npcs_have_stable_definitions() -> None:
    state = default_npcs_state()

    assert set(state) == {"old_caretaker", "library_ghost", "crypt_warden"}
    assert state["old_caretaker"]["location"] == "entry_hall"
    assert state["library_ghost"]["location"] == "library"
    assert state["crypt_warden"]["location"] == "crypt"


def test_normalization_migrates_legacy_room_and_preserves_richer_values() -> None:
    state = {
        "npcs": {
            "ghost": {"room": "library"},
            "keeper": {
                "name": "Keeper",
                "location": "grand_corridor",
                "status": "watching",
                "disposition": "wary",
                "aliases": ["old keeper"],
                "tags": ["human"],
            },
            "broken": "not an entity",
        }
    }

    npcs = ensure_npcs_state(state)

    assert npcs["ghost"] == {
        "id": "ghost",
        "name": "Ghost",
        "description": "",
        "location": "library",
        "status": "active",
        "disposition": "neutral",
        "aliases": [],
        "tags": [],
    }
    assert npcs["keeper"]["status"] == "watching"
    assert npcs["keeper"]["disposition"] == "wary"
    assert npcs["keeper"]["aliases"] == ["old keeper"]
    assert "broken" not in npcs
    assert "room" not in npcs["ghost"]


def test_normalization_does_not_backfill_development_npcs() -> None:
    state = {"npcs": {}}

    ensure_npcs_state(state)

    assert state["npcs"] == {}


def test_nearby_lookup_and_resolution_are_room_scoped() -> None:
    npcs = {
        "caretaker": {
            "id": "caretaker",
            "name": "Old Caretaker",
            "location": "entry_hall",
            "status": "active",
            "aliases": ["old man"],
            "tags": ["human"],
        },
        "ghost": {
            "id": "ghost",
            "name": "Library Ghost",
            "location": "library",
            "status": "active",
            "aliases": ["apparition"],
            "tags": ["undead"],
        },
    }

    assert [npc.id for npc in nearby_npcs_for_room(npcs, "entry_hall")] == ["caretaker"]
    assert resolve_npc_ids(npcs, "old man", "entry_hall") == ["caretaker"]
    assert resolve_npc_ids(npcs, "ghost", "entry_hall") == []


def test_nearby_npcs_use_stable_id_order_and_filter_absent_entities() -> None:
    npcs = {
        "warden": {"name": "Warden", "location": "entry_hall", "status": "active"},
        "caretaker": {"name": "Caretaker", "location": "entry_hall", "status": "active"},
        "ghost": {"name": "Ghost", "location": "entry_hall", "status": "absent"},
    }

    assert nearby_npc_ids_for_room(npcs, "entry_hall") == ["caretaker", "warden"]
    assert [npc.id for npc in nearby_npcs_for_room(npcs, "entry_hall")] == [
        "caretaker",
        "warden",
    ]


def test_fresh_state_gets_development_npcs_and_round_trips() -> None:
    executor = _executor()
    state, result = executor.execute(
        parsed_action=ParsedAction(raw_text="look", action=ActionType.OBSERVE, parse_status="ok"),
        campaign_state="No campaign state yet.",
    )

    assert result.success is True
    assert [npc.id for npc in result.nearby_npcs] == ["old_caretaker"]
    reloaded = json.loads(json.dumps(state))
    assert reloaded["npcs"]["old_caretaker"]["location"] == "entry_hall"
    assert reloaded["npcs"]["old_caretaker"]["status"] == "active"


def test_player_executor_does_not_expose_world_authority_methods() -> None:
    executor = _executor()

    assert not hasattr(executor, "spawn_npc")
    assert not hasattr(executor, "record_fact")


def test_observe_and_move_exclude_remote_npcs() -> None:
    executor = _executor()
    campaign_state = json.dumps(
        {
            "player": {"location": "entry_hall", "inventory": []},
            "npcs": {
                "here": {"name": "Here", "location": "entry_hall"},
                "away": {"name": "Away", "location": "library"},
            },
        }
    )

    _, observe_result = executor.execute(
        parsed_action=ParsedAction(raw_text="look", action=ActionType.OBSERVE, parse_status="ok"),
        campaign_state=campaign_state,
    )
    _, move_result = executor.execute(
        parsed_action=ParsedAction(
            raw_text="go north", action=ActionType.MOVE, target="north", parse_status="ok"
        ),
        campaign_state=campaign_state,
    )

    assert [npc.id for npc in observe_result.nearby_npcs] == ["here"]
    assert move_result.current_location == "grand_corridor"
    assert move_result.nearby_npcs == []


def test_parser_context_contains_structured_nearby_npcs_only() -> None:
    context = ActionParserAgent()._build_parser_context(
        json.dumps(
            {
                "player": {"location": "entry_hall"},
                "npcs": {
                    "warden": {
                        "name": "Warden",
                        "location": "entry_hall",
                        "aliases": [],
                    },
                    "caretaker": {
                        "name": "Caretaker",
                        "location": "entry_hall",
                        "aliases": ["keeper"],
                    },
                    "ghost": {
                        "name": "Ghost",
                        "location": "entry_hall",
                        "status": "absent",
                        "aliases": ["spirit"],
                    },
                },
            }
        )
    )

    assert context.nearby_npcs == [
        {"id": "caretaker", "name": "Caretaker", "aliases": ["keeper"]},
        {"id": "warden", "name": "Warden", "aliases": []},
    ]
