from __future__ import annotations

import copy

import pytest

from app.game.campaign_state import build_fresh_campaign_state
from app.schemas.world import (
    AdvanceClockWorldAction,
    MoveNpcWorldAction,
    RecordFactWorldAction,
    SetNpcStatusWorldAction,
)
from app.services.world_authority import WorldAuthorityExecutor


def test_world_authority_move_npc_updates_location_and_is_non_mutating_on_failure() -> None:
    state = build_fresh_campaign_state()
    executor = WorldAuthorityExecutor()

    action = MoveNpcWorldAction(npc_id="old_caretaker", destination_room_id="grand_corridor")
    next_state, result = executor.execute(action, state)

    assert result.success is True
    assert result.changed is True
    assert next_state["npcs"]["old_caretaker"]["location"] == "grand_corridor"
    assert state["npcs"]["old_caretaker"]["location"] == "entry_hall"
    assert result.state_delta == {
        "npcs": {
            "old_caretaker": {
                "location": {"from": "entry_hall", "to": "grand_corridor"}
            }
        }
    }

    invalid_state = copy.deepcopy(state)
    invalid_result = executor.execute(
        MoveNpcWorldAction(npc_id="old_caretaker", destination_room_id="library"),
        invalid_state,
    )[1]
    assert invalid_result.success is False
    assert invalid_result.error_code == "destination_room_not_adjacent"
    assert invalid_state["npcs"]["old_caretaker"]["location"] == "entry_hall"


def test_world_authority_status_noop_and_reset_semantics() -> None:
    state = build_fresh_campaign_state()
    executor = WorldAuthorityExecutor()

    no_op, result = executor.execute(SetNpcStatusWorldAction(npc_id="old_caretaker", status="active"), state)
    assert result.success is True
    assert result.changed is False
    assert result.state_delta == {}
    assert no_op["npcs"]["old_caretaker"]["status"] == "active"

    changed, changed_result = executor.execute(
        SetNpcStatusWorldAction(npc_id="old_caretaker", status="absent"),
        state,
    )
    assert changed_result.success is True
    assert changed_result.changed is True
    assert changed["npcs"]["old_caretaker"]["status"] == "absent"
    assert changed_result.state_delta == {
        "npcs": {
            "old_caretaker": {
                "status": {"from": "active", "to": "absent"}
            }
        }
    }


def test_world_authority_clock_and_fact_semantics() -> None:
    state = build_fresh_campaign_state()
    executor = WorldAuthorityExecutor()

    next_state, clock_result = executor.execute(
        AdvanceClockWorldAction(ticks=3),
        state,
    )
    assert clock_result.success is True
    assert next_state["clock"]["tick"] == 3
    assert clock_result.state_delta == {"clock": {"tick": {"from": 0, "to": 3}}}

    fact_state, fact_result = executor.execute(
        RecordFactWorldAction(fact="  A notable clue  "),
        next_state,
    )
    assert fact_result.success is True
    assert fact_result.changed is True
    assert fact_state["facts"] == ["A notable clue"]
    assert fact_result.state_delta == {"facts": {"added": ["A notable clue"]}}

    duplicate_state, duplicate_result = executor.execute(
        RecordFactWorldAction(fact="A notable clue"),
        fact_state,
    )
    assert duplicate_result.success is True
    assert duplicate_result.changed is False
    assert duplicate_result.state_delta == {}
    assert duplicate_state["facts"] == ["A notable clue"]


def test_world_authority_rejects_invalid_raw_dict_action_without_mutating_original() -> None:
    executor = WorldAuthorityExecutor()
    state = build_fresh_campaign_state()
    original = copy.deepcopy(state)

    result = executor.execute({"action": "move_npc", "npc_id": 42, "destination_room_id": "grand_corridor"}, state)[1]

    assert result.success is False
    assert result.error_code == "invalid_world_action"
    assert state == original


def test_world_authority_rejects_malformed_existing_npc_status_without_mutating_original() -> None:
    executor = WorldAuthorityExecutor()
    state = build_fresh_campaign_state()
    state["npcs"]["old_caretaker"]["status"] = ["broken"]
    original = copy.deepcopy(state)

    result = executor.execute(SetNpcStatusWorldAction(npc_id="old_caretaker", status="active"), state)[1]

    assert result.success is False
    assert result.error_code == "invalid_npc_status"
    assert state == original


def test_world_authority_rejects_malformed_state_without_mutating_original() -> None:
    executor = WorldAuthorityExecutor()
    invalid_state = {"clock": {"tick": "broken"}, "npcs": {}}
    original = copy.deepcopy(invalid_state)

    result = executor.execute(AdvanceClockWorldAction(ticks=1), invalid_state)[1]

    assert result.success is False
    assert result.error_code == "invalid_clock_state"
    assert invalid_state == original


def test_world_authority_rejects_fact_limit_and_duplicate_bounds() -> None:
    executor = WorldAuthorityExecutor()
    state = {"facts": [f"fact-{index}" for index in range(256)]}
    original = copy.deepcopy(state)

    result = executor.execute(RecordFactWorldAction(fact="fact-255"), state)[1]
    assert result.success is True
    assert result.changed is False

    fail = executor.execute(RecordFactWorldAction(fact="new fact"), state)[1]
    assert fail.success is False
    assert fail.error_code == "facts_limit_reached"
    assert state == original


@pytest.mark.parametrize(
    ("action", "error_code"),
    [
        (MoveNpcWorldAction(npc_id="missing_npc", destination_room_id="grand_corridor"), "npc_not_found"),
        (MoveNpcWorldAction(npc_id="old_caretaker", destination_room_id="missing_room"), "destination_room_not_found"),
    ],
)
def test_world_authority_move_npc_rejects_missing_entities_without_mutating_state(
    action: MoveNpcWorldAction,
    error_code: str,
) -> None:
    executor = WorldAuthorityExecutor()
    state = build_fresh_campaign_state()
    original = copy.deepcopy(state)

    _, result = executor.execute(action, state)

    assert result.success is False
    assert result.error_code == error_code
    assert state == original


@pytest.mark.parametrize("action", [
    MoveNpcWorldAction(npc_id="old_caretaker", destination_room_id="grand_corridor"),
    SetNpcStatusWorldAction(npc_id="old_caretaker", status="absent"),
])
def test_world_authority_rejects_structurally_malformed_npc_entries_without_mutating_state(
    action: MoveNpcWorldAction | SetNpcStatusWorldAction,
) -> None:
    executor = WorldAuthorityExecutor()
    state = build_fresh_campaign_state()
    state["npcs"]["old_caretaker"] = "corrupt"
    original = copy.deepcopy(state)

    _, result = executor.execute(action, state)

    assert result.success is False
    assert result.error_code == "malformed_npc_state"
    assert state == original


@pytest.mark.parametrize("invalid_location", [None, "missing_room"])
def test_world_authority_rejects_invalid_npc_locations_without_mutating_state(
    invalid_location: str | None,
) -> None:
    executor = WorldAuthorityExecutor()
    state = build_fresh_campaign_state()
    state["npcs"]["old_caretaker"]["location"] = invalid_location
    original = copy.deepcopy(state)

    _, result = executor.execute(
        MoveNpcWorldAction(npc_id="old_caretaker", destination_room_id="grand_corridor"),
        state,
    )

    assert result.success is False
    assert result.error_code == "invalid_npc_location"
    assert state == original


def test_world_authority_moves_absent_npc_without_changing_status() -> None:
    executor = WorldAuthorityExecutor()
    state = build_fresh_campaign_state()
    state["npcs"]["old_caretaker"]["status"] = "absent"

    next_state, result = executor.execute(
        MoveNpcWorldAction(npc_id="old_caretaker", destination_room_id="grand_corridor"),
        state,
    )

    assert result.success is True
    assert next_state["npcs"]["old_caretaker"]["location"] == "grand_corridor"
    assert next_state["npcs"]["old_caretaker"]["status"] == "absent"
    assert result.state_delta == {
        "npcs": {
            "old_caretaker": {
                "location": {"from": "entry_hall", "to": "grand_corridor"}
            }
        }
    }


def test_world_authority_status_round_trip_preserves_npc_location() -> None:
    executor = WorldAuthorityExecutor()
    state = build_fresh_campaign_state()
    state["npcs"]["old_caretaker"]["location"] = "grand_corridor"

    absent_state, absent_result = executor.execute(
        SetNpcStatusWorldAction(npc_id="old_caretaker", status="absent"),
        state,
    )
    active_state, active_result = executor.execute(
        SetNpcStatusWorldAction(npc_id="old_caretaker", status="active"),
        absent_state,
    )

    assert absent_result.success is True
    assert active_result.success is True
    assert active_state["npcs"]["old_caretaker"]["location"] == "grand_corridor"
    assert active_state["npcs"]["old_caretaker"]["status"] == "active"


@pytest.mark.parametrize("ticks", [0, 11])
def test_world_authority_rejects_clock_bounds_without_mutating_state(ticks: int) -> None:
    executor = WorldAuthorityExecutor()
    state = build_fresh_campaign_state()
    original = copy.deepcopy(state)

    _, result = executor.execute({"action": "advance_clock", "ticks": ticks}, state)

    assert result.success is False
    assert result.error_code == "invalid_world_action"
    assert state == original


@pytest.mark.parametrize("fact", ["   ", "x" * 513])
def test_world_authority_rejects_invalid_facts_without_mutating_state(fact: str) -> None:
    executor = WorldAuthorityExecutor()
    state = build_fresh_campaign_state()
    original = copy.deepcopy(state)

    _, result = executor.execute(RecordFactWorldAction(fact=fact), state)

    assert result.success is False
    assert result.error_code == "invalid_fact"
    assert state == original


@pytest.mark.parametrize("facts", [None, ["valid", 7]])
def test_world_authority_rejects_malformed_facts_without_mutating_state(facts: object) -> None:
    executor = WorldAuthorityExecutor()
    state = build_fresh_campaign_state()
    state["facts"] = facts
    original = copy.deepcopy(state)

    _, result = executor.execute(RecordFactWorldAction(fact="new fact"), state)

    assert result.success is False
    assert result.error_code == "malformed_facts_state"
    assert state == original


def test_world_authority_enforces_unique_fact_capacity_with_preexisting_duplicates() -> None:
    executor = WorldAuthorityExecutor()
    facts = [f"fact-{index}" for index in range(255)] + ["fact-0"]
    state = {"facts": facts}

    expanded_state, expanded_result = executor.execute(RecordFactWorldAction(fact="fact-255"), state)
    assert expanded_result.success is True
    assert expanded_result.changed is True
    assert len(expanded_state["facts"]) == 257
    assert len(set(expanded_state["facts"])) == 256

    original = copy.deepcopy(expanded_state)
    _, limit_result = executor.execute(RecordFactWorldAction(fact="another fact"), expanded_state)
    assert limit_result.success is False
    assert limit_result.error_code == "facts_limit_reached"
    assert expanded_state == original


def test_world_authority_accepts_only_action_as_raw_dict_discriminator() -> None:
    executor = WorldAuthorityExecutor()
    state = build_fresh_campaign_state()
    original = copy.deepcopy(state)

    _, result = executor.execute(
        {
            "type": "move_npc",
            "npc_id": "old_caretaker",
            "destination_room_id": "grand_corridor",
        },
        state,
    )

    assert result.success is False
    assert result.error_code == "invalid_world_action"
    assert state == original
