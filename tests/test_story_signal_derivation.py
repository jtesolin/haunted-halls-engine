from __future__ import annotations

import pytest

from app.game.story import apply_story_signal, derive_story_signal
from app.schemas.chat import ToolExecutionResult
from app.schemas.generated_abilities import (
    AbilityGameplayResult,
    AbilityGameplayStatus,
    AbilityObjectEffect,
    AbilityObjectEffectOperation,
    AbilityPresenceEffect,
    AbilitySenseFilter,
    AbilitySensingScope,
)
from app.schemas.story import ItemAcquiredSignal, NpcSpokenToSignal, RoomEnteredSignal


def test_derive_story_signal_from_successful_movement() -> None:
    signal = derive_story_signal(
        ToolExecutionResult(
            success=True,
            applied_tools=["move_player"],
            summary="You move east.",
            current_location="library",
            resolved_exit="library",
        ),
    )

    assert signal == RoomEnteredSignal(room_id="library")


def test_derive_story_signal_from_successful_talk_and_take() -> None:
    talk_signal = derive_story_signal(
        ToolExecutionResult(
            success=True,
            applied_tools=["talk_to_npc"],
            summary="You address the ghost.",
            npc_id="library_ghost",
        ),
    )
    take_signal = derive_story_signal(
        ToolExecutionResult(
            success=True,
            applied_tools=["take_item"],
            summary="You take the old book.",
            item_id="old_book",
        ),
    )

    assert talk_signal == NpcSpokenToSignal(npc_id="library_ghost")
    assert take_signal == ItemAcquiredSignal(item_id="old_book")


def test_derive_story_signal_ignores_failed_or_uncertain_results() -> None:
    assert derive_story_signal(ToolExecutionResult(success=False, summary="No move.")) is None
    assert (
        derive_story_signal(
            ToolExecutionResult(
                success=True,
                applied_tools=["take_item"],
                summary="You try to take the old book.",
                item_id=None,
            ),
        )
        is None
    )
    assert (
        derive_story_signal(
            ToolExecutionResult(
                success=True,
                applied_tools=["move_player"],
                summary="You move somewhere.",
                current_location="library",
            )
        )
        is None
    )


def test_drop_does_not_produce_item_acquired_signal() -> None:
    assert (
        derive_story_signal(
            ToolExecutionResult(
                success=True,
                applied_tools=["drop_item"],
                summary="You drop the old book.",
                item_id="old_book",
            )
        )
        is None
    )


@pytest.mark.parametrize("operation", list(AbilityObjectEffectOperation))
def test_generated_object_effect_emits_acquisition_only_for_retrieve(
    operation: AbilityObjectEffectOperation,
) -> None:
    result = ToolExecutionResult(
        success=True,
        applied_tools=["resolve_generated_ability"],
        summary="Generated object effect resolved.",
        ability_result=AbilityGameplayResult(
            ability_id="whispering_touch", owned=True, available=True,
            status=AbilityGameplayStatus.RESOLVED,
            object_effect=AbilityObjectEffect(
                operation=operation, item_id="old_book", item_name="Old Book",
            ),
        ),
    )
    expected = ItemAcquiredSignal(item_id="old_book") if operation == AbilityObjectEffectOperation.RETRIEVE else None
    assert derive_story_signal(result) == expected
    assert result.applied_tools == ["resolve_generated_ability"]
    assert result.item_id is None


def test_generated_sensory_effect_does_not_emit_acquisition() -> None:
    assert derive_story_signal(ToolExecutionResult(
        success=True, applied_tools=["resolve_generated_ability"], summary="Presence sensed.",
        ability_result=AbilityGameplayResult(
            ability_id="echo_sense", status=AbilityGameplayStatus.RESOLVED,
            presence_effect=AbilityPresenceEffect(
                sense_filter=AbilitySenseFilter.PRESENCE,
                scope=AbilitySensingScope.CURRENT_ROOM, found=False,
                current_room_count=0, adjacent_room_count=0,
            ),
        ),
    )) is None


@pytest.mark.parametrize("status", [
    AbilityGameplayStatus.INELIGIBLE_CONTEXT,
    AbilityGameplayStatus.UNAVAILABLE,
    AbilityGameplayStatus.UNSUPPORTED,
])
@pytest.mark.parametrize("has_effect", [False, True])
def test_failed_generated_retrieve_does_not_emit_acquisition(
    status: AbilityGameplayStatus, has_effect: bool,
) -> None:
    assert derive_story_signal(ToolExecutionResult(
        success=False, applied_tools=["resolve_generated_ability"], summary="Cannot retrieve.",
        ability_result=AbilityGameplayResult(
            ability_id="whispering_touch", status=status,
            object_effect=AbilityObjectEffect(
                operation=AbilityObjectEffectOperation.RETRIEVE,
                item_id="old_book", item_name="Old Book",
            ) if has_effect else None,
        ),
    )) is None


@pytest.mark.parametrize("object_effect", [
    None,
    AbilityObjectEffect(
        operation=AbilityObjectEffectOperation.RETRIEVE, item_id="", item_name="Old Book",
    ),
])
def test_generated_result_without_retrieve_identity_does_not_emit_acquisition(
    object_effect: AbilityObjectEffect | None,
) -> None:
    assert derive_story_signal(ToolExecutionResult(
        success=True, applied_tools=["resolve_generated_ability"], summary="No acquisition.",
        ability_result=AbilityGameplayResult(
            ability_id="whispering_touch", status=AbilityGameplayStatus.RESOLVED,
            object_effect=object_effect,
        ),
    )) is None


def test_generated_tool_without_ability_result_does_not_emit_acquisition() -> None:
    assert derive_story_signal(ToolExecutionResult(
        success=True, applied_tools=["resolve_generated_ability"],
        item_id="old_book", summary="No typed effect.",
    )) is None


def test_failed_take_does_not_emit_acquisition() -> None:
    assert derive_story_signal(ToolExecutionResult(
        success=False, applied_tools=["take_item"], item_id="old_book", summary="Cannot take.",
    )) is None


@pytest.mark.parametrize(
    ("tools", "expected"),
    [
        (["move_player", "talk_to_npc", "take_item"], RoomEnteredSignal(room_id="library")),
        (["talk_to_npc", "take_item"], NpcSpokenToSignal(npc_id="library_ghost")),
        (["take_item"], ItemAcquiredSignal(item_id="brass_key")),
    ],
)
def test_existing_signal_precedence_is_preserved_with_generated_effect(
    tools: list[str], expected: RoomEnteredSignal | NpcSpokenToSignal | ItemAcquiredSignal,
) -> None:
    assert derive_story_signal(ToolExecutionResult(
        success=True, applied_tools=tools, summary="Authoritative result.",
        resolved_exit="library", npc_id="library_ghost", item_id="brass_key",
        ability_result=AbilityGameplayResult(
            ability_id="whispering_touch", status=AbilityGameplayStatus.RESOLVED,
            object_effect=AbilityObjectEffect(
                operation=AbilityObjectEffectOperation.RETRIEVE, item_id="old_book", item_name="Old Book",
            ),
        ),
    )) == expected


def test_story_progression_is_idempotent_for_duplicate_matches() -> None:
    state = {
        "player": {"location": "entry_hall", "inventory": []},
        "items": {},
        "npcs": {},
        "clock": {"tick": 0},
        "facts": [],
    }
    first = apply_story_signal(state, RoomEnteredSignal(room_id="library"))
    second = apply_story_signal(state, RoomEnteredSignal(room_id="library"))

    assert first.changed is True
    assert second.changed is False
    assert state["story"]["quests"]["librarys_whisper"]["objectives"]["enter_library"] == "completed"
