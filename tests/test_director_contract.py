from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from app.game.campaign_state import build_fresh_campaign_state
from app.game.abilities import CANONICAL_ABILITY_DEFINITIONS
from app.game.character_progression import (
    PROGRESSION_TRACK_IDS,
    grant_progress,
    unlock_ability,
)
from app.game.story import STORY_QUESTS, apply_story_signal
from app.schemas.chat import ActionType, ParsedAction, ParseStatus, ToolExecutionResult
from app.schemas.story import ItemAcquiredSignal, NpcSpokenToSignal, RoomEnteredSignal
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
    RevealClueWorldAction,
    SetNpcStatusWorldAction,
    WorldAction,
)
from app.services.director_context import (
    InvalidDirectorContextError,
    build_director_input,
)


PROPOSAL_ADAPTER = TypeAdapter(DirectorProposal)
WORLD_ACTION_ADAPTER = TypeAdapter(WorldAction)


def test_package_schemas_exports_reveal_clue_world_action() -> None:
    """Package-level world-action exports include the executor-supported clue action."""
    from app import schemas
    from app.schemas import (
        AdvanceClockWorldAction as PackageAdvanceClockWorldAction,
        RevealClueWorldAction as PackageRevealClueWorldAction,
    )

    assert PackageRevealClueWorldAction is RevealClueWorldAction
    assert PackageAdvanceClockWorldAction is AdvanceClockWorldAction
    assert "RevealClueWorldAction" in schemas.__all__
    assert PackageRevealClueWorldAction(
        clue_id="ghost_points_to_old_book"
    ).action == "reveal_clue"


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
        RevealClueWorldAction(clue_id="ghost_points_to_old_book"),
    ],
)
def test_director_accepts_exactly_one_promoted_world_action(
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
        {"action": "advance_story_beat", "quest_id": "librarys_whisper"},
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


def test_director_proposal_rejects_story_and_world_flag_actions() -> None:
    for action in (
        {"action": "set_world_flag", "flag": "cellar_open"},
        {"action": "advance_story_beat", "quest_id": "librarys_whisper"},
    ):
        with pytest.raises(ValidationError):
            PROPOSAL_ADAPTER.validate_python(
                {"decision": "act", "world_action": action}
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
    assert [quest.quest_id for quest in context.story.quests] == ["librarys_whisper"]
    assert [track.track_id.value for track in context.character.progression_tracks] == list(
        PROGRESSION_TRACK_IDS
    )
    assert context.narrative.revealable_clues == []


def test_director_context_projects_initial_story_without_locked_future_details() -> None:
    state = build_fresh_campaign_state()

    context = _director_input(state)

    assert len(context.story.quests) == 1
    quest = context.story.quests[0]
    assert quest.quest_id == "librarys_whisper"
    assert quest.title == "The Library's Whisper"
    assert quest.status.value == "active"
    assert quest.completed_objective_ids == []
    assert quest.active_objective is not None
    assert quest.active_objective.objective_id == "enter_library"
    assert quest.active_objective.description == "Enter the library."

    serialized = context.model_dump_json(exclude_none=True)
    assert "Speak to the library ghost." not in serialized
    assert "Acquire the old book." not in serialized
    assert "signal_type" not in serialized
    assert "match_value" not in serialized


def test_director_context_projects_progressed_story_from_domain_state() -> None:
    state = build_fresh_campaign_state()

    apply_story_signal(state, RoomEnteredSignal(room_id="library"))
    context = _director_input(state)
    quest = context.story.quests[0]
    assert quest.completed_objective_ids == ["enter_library"]
    assert quest.active_objective is not None
    assert quest.active_objective.objective_id == "speak_to_library_ghost"

    apply_story_signal(state, NpcSpokenToSignal(npc_id="library_ghost"))
    context = _director_input(state)
    quest = context.story.quests[0]
    assert quest.completed_objective_ids == [
        "enter_library",
        "speak_to_library_ghost",
    ]
    assert quest.active_objective is not None
    assert quest.active_objective.objective_id == "acquire_old_book"
    assert [clue.clue_id for clue in context.narrative.revealable_clues] == [
        "ghost_points_to_old_book"
    ]
    assert context.narrative.revealable_clues[0].text == (
        "The library ghost's attention settles on the old book."
    )

    apply_story_signal(state, ItemAcquiredSignal(item_id="old_book"))
    context = _director_input(state)
    quest = context.story.quests[0]
    assert quest.status.value == "completed"
    assert quest.completed_objective_ids == [
        "enter_library",
        "speak_to_library_ghost",
        "acquire_old_book",
    ]
    assert quest.active_objective is None
    assert context.narrative.revealable_clues == []


def test_director_narrative_context_only_contains_eligible_unrevealed_clues() -> None:
    state = build_fresh_campaign_state()
    original = copy.deepcopy(state)

    before_context = _director_input(state)
    assert before_context.narrative.revealable_clues == []

    apply_story_signal(state, RoomEnteredSignal(room_id="library"))
    assert _director_input(state).narrative.revealable_clues == []

    apply_story_signal(state, NpcSpokenToSignal(npc_id="library_ghost"))
    eligible_context = _director_input(state)
    repeated_context = _director_input(state)

    assert eligible_context.narrative == repeated_context.narrative
    assert [clue.clue_id for clue in eligible_context.narrative.revealable_clues] == [
        "ghost_points_to_old_book"
    ]
    assert [clue.clue_id for clue in eligible_context.narrative.revealable_clues] == sorted(
        clue.clue_id for clue in eligible_context.narrative.revealable_clues
    )
    assert "narrative" not in original
    assert "narrative" not in state

    state["narrative"] = {"revealed_clues": ["ghost_points_to_old_book"]}
    revealed_context = _director_input(state)
    assert revealed_context.narrative.revealable_clues == []


def test_director_context_projects_character_capabilities_only_when_available() -> None:
    state = build_fresh_campaign_state()
    grant_progress(state, "investigation", 2)
    grant_progress(state, "rapport", 1)
    unlock_ability(state, "keen_eye")
    unlock_ability(state, "read_the_room")

    context = _director_input(state)

    assert [track.track_id.value for track in context.character.progression_tracks] == list(
        PROGRESSION_TRACK_IDS
    )
    assert [track.points for track in context.character.progression_tracks] == [2, 0, 1, 0]
    assert [ability.ability_id for ability in context.character.available_abilities] == [
        "keen_eye"
    ]
    ability = context.character.available_abilities[0]
    definition = CANONICAL_ABILITY_DEFINITIONS[0]
    assert ability.display_name == definition.display_name
    assert ability.short_description == definition.short_description
    assert ability.track_id == definition.track


def test_director_context_malformed_story_and_progression_remain_safe_and_non_mutating() -> None:
    state = build_fresh_campaign_state()
    state["story"] = {
        "quests": {
            "unknown_quest": {"status": "completed", "objectives": {}},
            "librarys_whisper": {
                "status": "completed",
                "objectives": {
                    "enter_library": "locked",
                    "speak_to_library_ghost": "completed",
                    "acquire_old_book": "locked",
                },
            },
        }
    }
    state["player"]["progression"] = {
        "version": 999,
        "tracks": {
            "investigation": 999,
            "resolve": -1,
            "rapport": "many",
            "occult": True,
        },
        "unlocked_abilities": ["keen_eye", "unknown_ability"],
    }
    original = copy.deepcopy(state)

    context = _director_input(state)

    quest = context.story.quests[0]
    assert quest.quest_id == "librarys_whisper"
    assert quest.status.value == "active"
    assert quest.completed_objective_ids == []
    assert quest.active_objective is not None
    assert quest.active_objective.objective_id == "enter_library"
    assert [track.points for track in context.character.progression_tracks] == [0, 0, 0, 0]
    assert context.character.available_abilities == []
    assert "unknown_quest" not in context.model_dump_json(exclude_none=True)
    assert "unknown_ability" not in context.model_dump_json(exclude_none=True)
    assert state == original


def test_director_context_projection_ordering_is_stable() -> None:
    state = build_fresh_campaign_state()
    apply_story_signal(state, RoomEnteredSignal(room_id="library"))
    apply_story_signal(state, NpcSpokenToSignal(npc_id="library_ghost"))
    grant_progress(state, "occult", 2)
    grant_progress(state, "resolve", 2)
    unlock_ability(state, "occult_insight")
    unlock_ability(state, "steady_nerves")

    context = _director_input(state)
    repeated = _director_input(state)

    assert context == repeated
    assert [quest.quest_id for quest in context.story.quests] == list(STORY_QUESTS)
    assert context.story.quests[0].completed_objective_ids == [
        "enter_library",
        "speak_to_library_ghost",
    ]
    assert [track.track_id.value for track in context.character.progression_tracks] == list(
        PROGRESSION_TRACK_IDS
    )
    assert [ability.ability_id for ability in context.character.available_abilities] == [
        "steady_nerves",
        "occult_insight",
    ]


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


def test_broad_world_action_accepts_reveal_clue() -> None:
    """Broad WorldAction union accepts reveal_clue."""
    from app.schemas.world import RevealClueWorldAction

    action = RevealClueWorldAction(clue_id="ghost_points_to_old_book")
    validated = WORLD_ACTION_ADAPTER.validate_python(action.model_dump())

    assert isinstance(validated, RevealClueWorldAction)
    assert validated.clue_id == "ghost_points_to_old_book"


def test_director_proposal_accepts_reveal_clue() -> None:
    """DirectorProposal accepts reveal_clue during 8D4."""
    reveal_action = {
        "action": "reveal_clue",
        "clue_id": "ghost_points_to_old_book",
    }

    proposal = PROPOSAL_ADAPTER.validate_python(
        {"decision": "act", "world_action": reveal_action}
    )

    assert isinstance(proposal, WorldActionProposal)
    assert isinstance(proposal.world_action, RevealClueWorldAction)
    assert proposal.world_action.clue_id == "ghost_points_to_old_book"


def test_director_proposal_still_rejects_other_unsupported_actions() -> None:
    """DirectorProposal continues to reject non-Director actions."""
    unsupported_actions = [
        {"action": "spawn_npc", "npc_id": "new_npc"},
        {"action": "set_world_flag", "flag": "visited_library"},
        {"action": "advance_story_beat", "quest_id": "librarys_whisper"},
    ]

    for action in unsupported_actions:
        with pytest.raises(ValidationError):
            PROPOSAL_ADAPTER.validate_python(
                {"decision": "act", "world_action": action}
            )


def test_director_proposal_still_accepts_four_supported_actions() -> None:
    """DirectorProposal continues to accept the four Director actions."""
    from app.schemas.world import (
        MoveNpcWorldAction,
        SetNpcStatusWorldAction,
        AdvanceClockWorldAction,
        RecordFactWorldAction,
    )

    actions = [
        MoveNpcWorldAction(
            npc_id="old_caretaker", destination_room_id="grand_corridor"
        ),
        SetNpcStatusWorldAction(npc_id="old_caretaker", status="absent"),
        AdvanceClockWorldAction(ticks=1),
        RecordFactWorldAction(fact="A fact"),
    ]

    for action in actions:
        proposal = PROPOSAL_ADAPTER.validate_python(
            {"decision": "act", "world_action": action.model_dump()}
        )
        assert isinstance(proposal, WorldActionProposal)
