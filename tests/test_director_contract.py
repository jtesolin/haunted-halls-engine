from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from app.game.campaign_state import build_fresh_campaign_state
from app.schemas.chat import ActionType, ParsedAction, ParseStatus, ToolExecutionResult
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
    WorldAction,
)
from app.services.director_context import (
    InvalidDirectorContextError,
    build_director_input,
)


PROPOSAL_ADAPTER = TypeAdapter(DirectorProposal)
WORLD_ACTION_ADAPTER = TypeAdapter(WorldAction)


def _parsed_action(
    *,
    action: ActionType = ActionType.OBSERVE,
    target: str | None = None,
    parse_status: ParseStatus = "ok",
) -> ParsedAction:
    return ParsedAction(
        raw_text="look around",
        action=action,
        target=target,
        parse_status=parse_status,
    )


def _tool_result(
    *,
    success: bool = True,
    summary: str = "You take stock of Entry Hall.",
    applied_tools: list[str] | None = None,
    error_code: str | None = None,
) -> ToolExecutionResult:
    return ToolExecutionResult(
        success=success,
        summary=summary,
        applied_tools=["observe"] if applied_tools is None else applied_tools,
        error_code=error_code,
    )


def _director_input(state: dict[str, Any] | None = None) -> DirectorInput:
    return build_director_input(
        state or build_fresh_campaign_state(),
        parsed_action=_parsed_action(),
        tool_result=_tool_result(),
    )


def test_director_no_action_proposal_is_valid_and_strict() -> None:
    proposal = PROPOSAL_ADAPTER.validate_python({"decision": "none"})

    assert isinstance(proposal, NoActionProposal)
    with pytest.raises(ValidationError):
        PROPOSAL_ADAPTER.validate_python({"decision": "none", "world_action": None})


def test_director_proposal_rejects_missing_conflicting_and_extra_fields() -> None:
    valid_action = MoveNpcWorldAction(
        npc_id="old_caretaker",
        destination_room_id="grand_corridor",
    ).model_dump()

    invalid_payloads = [
        {"decision": "act"},
        {"decision": "none", "world_action": valid_action},
        {"decision": "none", "reason": "quiet moment"},
        {"decision": "act", "world_action": valid_action, "priority": "high"},
    ]

    for payload in invalid_payloads:
        with pytest.raises(ValidationError):
            PROPOSAL_ADAPTER.validate_python(payload)


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


def test_director_proposal_rejects_malformed_parameters_through_world_action_schema() -> None:
    malformed_action = {
        "action": "move_npc",
        "npc_id": "",
        "destination_room_id": "grand_corridor",
    }

    with pytest.raises(ValidationError):
        WORLD_ACTION_ADAPTER.validate_python(malformed_action)
    with pytest.raises(ValidationError):
        PROPOSAL_ADAPTER.validate_python(
            {"decision": "act", "world_action": malformed_action}
        )


def test_director_context_is_bounded_deterministic_and_non_mutating() -> None:
    state = build_fresh_campaign_state()
    original = copy.deepcopy(state)
    context = _director_input(state)
    repeated_context = _director_input(state)

    assert context.current_player_room_id == "entry_hall"
    assert repeated_context == context
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


def test_director_context_includes_absent_npcs_with_canonical_ids_and_locations() -> None:
    state = build_fresh_campaign_state()
    state["player"]["location"] = "grand_corridor"
    state["npcs"]["old_caretaker"]["status"] = "absent"

    context = _director_input(state)

    assert context.current_player_room_id == "grand_corridor"
    caretaker = next(npc for npc in context.npcs if npc.npc_id == "old_caretaker")
    assert caretaker.npc_id == "old_caretaker"
    assert caretaker.location_id == "entry_hall"
    assert caretaker.status == "absent"


def test_director_context_projects_compact_player_result_fields() -> None:
    state = build_fresh_campaign_state()
    context = build_director_input(
        state,
        parsed_action=_parsed_action(
            action=ActionType.TALK,
            target="ghost",
            parse_status="ok",
        ),
        tool_result=_tool_result(
            success=False,
            summary="The ghost is not nearby.",
            applied_tools=[],
            error_code="npc_not_nearby",
        ),
    )

    assert context.player_action.action == ActionType.TALK
    assert context.player_action.target == "ghost"
    assert context.player_action.parse_status == "ok"
    assert context.player_action.succeeded is False
    assert context.player_action.result_summary == "The ghost is not nearby."
    assert context.player_action.applied_tools == []
    assert context.player_action.error_code == "npc_not_nearby"


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
            parsed_action=_parsed_action(action=ActionType.WAIT),
            tool_result=_tool_result(
                summary="Advanced clock.",
                applied_tools=["advance_clock"],
            ),
        )


@pytest.mark.parametrize(
    ("npc_value", "expected_message"),
    [
        ("corrupt", "Authoritative NPC 'old_caretaker' entry is malformed."),
        (
            {"location": "missing_room", "status": "active"},
            "Authoritative NPC 'old_caretaker' location is invalid.",
        ),
        (
            {"location": "entry_hall", "status": "broken"},
            "Authoritative NPC 'old_caretaker' status is invalid.",
        ),
    ],
)
def test_director_context_identifies_malformed_npc_by_canonical_id(
    npc_value: Any,
    expected_message: str,
) -> None:
    state = build_fresh_campaign_state()
    state["npcs"]["old_caretaker"] = npc_value

    with pytest.raises(InvalidDirectorContextError, match=expected_message):
        _director_input(state)
