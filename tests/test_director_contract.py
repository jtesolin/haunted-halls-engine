from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from app.game.campaign_state import build_fresh_campaign_state
from app.schemas.chat import ActionType, ParsedAction, ToolExecutionResult
from app.schemas.director import (
    DirectorInput,
    DirectorProposal,
    NoActionProposal,
    WorldActionProposal,
)
from app.schemas.world import (
    AdvanceClockWorldAction,
    MoveNpcWorldAction,
    RecordFactWorldAction,
    SetNpcStatusWorldAction,
)
from app.services.director_context import (
    InvalidDirectorContextError,
    build_director_input,
)


PROPOSAL_ADAPTER = TypeAdapter(DirectorProposal)


def _director_input() -> DirectorInput:
    return build_director_input(
        build_fresh_campaign_state(),
        parsed_action=ParsedAction(
            raw_text="look around",
            action=ActionType.OBSERVE,
            parse_status="ok",
        ),
        tool_result=ToolExecutionResult(
            success=True,
            summary="You take stock of Entry Hall.",
            applied_tools=["observe"],
        ),
    )


def test_director_no_action_proposal_is_valid_and_strict() -> None:
    proposal = PROPOSAL_ADAPTER.validate_python({"decision": "none"})

    assert isinstance(proposal, NoActionProposal)
    with pytest.raises(ValidationError):
        PROPOSAL_ADAPTER.validate_python({"decision": "none", "world_action": None})


@pytest.mark.parametrize(
    "action",
    [
        MoveNpcWorldAction(
            npc_id="old_caretaker", destination_room_id="grand_corridor"
        ),
        SetNpcStatusWorldAction(npc_id="old_caretaker", status="absent"),
        AdvanceClockWorldAction(ticks=1),
        RecordFactWorldAction(fact="A notable clue"),
    ],
)
def test_director_accepts_exactly_one_existing_world_action(
    action: Any,
) -> None:
    proposal = PROPOSAL_ADAPTER.validate_python(
        {"decision": "act", "world_action": action.model_dump()}
    )

    assert isinstance(proposal, WorldActionProposal)
    assert proposal.world_action == action


def test_director_proposal_rejects_unknown_and_spawn_actions() -> None:
    for action in (
        {"action": "spawn_npc", "npc_id": "new_npc"},
        {"action": "unlock_exit", "room_id": "entry_hall"},
    ):
        with pytest.raises(ValidationError):
            PROPOSAL_ADAPTER.validate_python(
                {"decision": "act", "world_action": action}
            )


def test_director_context_is_bounded_deterministic_and_non_mutating() -> None:
    state = build_fresh_campaign_state()
    original = copy.deepcopy(state)
    context = _director_input()

    assert context.current_player_room_id == "entry_hall"
    assert context.clock_tick == 0
    assert context.facts == []
    assert [npc.npc_id for npc in context.npcs] == [
        "crypt_warden",
        "library_ghost",
        "old_caretaker",
    ]
    caretaker = next(npc for npc in context.npcs if npc.npc_id == "old_caretaker")
    assert caretaker.one_hop_destination_room_ids == ["grand_corridor"]
    assert state == original


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("player", {}),
        ("clock", {"tick": "broken"}),
        ("facts", ["valid", 42]),
        ("npcs", {"old_caretaker": {"location": "entry_hall", "status": "broken"}}),
    ],
)
def test_director_context_rejects_malformed_authoritative_state(field, value) -> None:
    state = build_fresh_campaign_state()
    state[field] = value

    with pytest.raises(InvalidDirectorContextError):
        build_director_input(
            state,
            parsed_action=ParsedAction(raw_text="wait", action=ActionType.WAIT, parse_status="ok"),
            tool_result=ToolExecutionResult(success=True, summary="Advanced clock."),
        )
