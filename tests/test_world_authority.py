from __future__ import annotations

import copy

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
    assert fact_result.state_delta == {"facts": {"from": [], "to": ["A notable clue"]}}

    duplicate_state, duplicate_result = executor.execute(
        RecordFactWorldAction(fact="A notable clue"),
        fact_state,
    )
    assert duplicate_result.success is True
    assert duplicate_result.changed is False
    assert duplicate_result.state_delta == {}
    assert duplicate_state["facts"] == ["A notable clue"]


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

    result = executor.execute(RecordFactWorldAction(fact="fact-255"), state)[1]
    assert result.success is True
    assert result.changed is False

    fail = executor.execute(RecordFactWorldAction(fact="new fact"), state)[1]
    assert fail.success is False
    assert fail.error_code == "facts_limit_reached"
