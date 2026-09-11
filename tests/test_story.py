from __future__ import annotations

import json

import pytest

from app.game.story import (
    STORY_QUESTS,
    ObjectiveDefinition,
    QuestDefinition,
    apply_story_signal,
    ensure_story_state,
    validate_story_definitions,
)
from app.schemas.story import (
    FactRecordedSignal,
    ItemAcquiredSignal,
    NpcSpokenToSignal,
    ObjectiveStatus,
    QuestStatus,
    RoomEnteredSignal,
    StorySignalType,
    StoryProgressionOutcome,
)

QUEST_ID = "librarys_whisper"


def _fresh_state() -> dict:
    return {
        "player": {"location": "entry_hall", "inventory": []},
        "items": {},
        "npcs": {},
        "clock": {"tick": 0},
        "facts": [],
    }


def test_default_story_state_exposes_development_quest_with_stable_ids() -> None:
    state = _fresh_state()

    story = ensure_story_state(state)

    assert QUEST_ID in STORY_QUESTS
    quest_def = STORY_QUESTS[QUEST_ID]
    assert [objective.id for objective in quest_def.objectives] == [
        "enter_library",
        "speak_to_library_ghost",
        "acquire_old_book",
    ]

    progress = story["quests"][QUEST_ID]
    assert progress["status"] == QuestStatus.ACTIVE.value
    assert progress["objectives"] == {
        "enter_library": ObjectiveStatus.ACTIVE.value,
        "speak_to_library_ghost": ObjectiveStatus.LOCKED.value,
        "acquire_old_book": ObjectiveStatus.LOCKED.value,
    }


def test_legacy_state_with_no_story_namespace_normalizes_safely() -> None:
    state = _fresh_state()
    assert "story" not in state

    story = ensure_story_state(state)

    assert state["story"] is story
    assert story["quests"][QUEST_ID]["status"] == QuestStatus.ACTIVE.value


def test_entering_library_completes_only_first_objective() -> None:
    state = _fresh_state()

    result = apply_story_signal(state, RoomEnteredSignal(room_id="library"))

    assert result.changed is True
    assert result.outcome is StoryProgressionOutcome.OBJECTIVE_ADVANCED
    assert result.quest_id == QUEST_ID
    assert result.objective_id == "enter_library"

    progress = state["story"]["quests"][QUEST_ID]
    assert progress["objectives"]["enter_library"] == ObjectiveStatus.COMPLETED.value
    assert progress["objectives"]["speak_to_library_ghost"] == ObjectiveStatus.ACTIVE.value
    assert progress["objectives"]["acquire_old_book"] == ObjectiveStatus.LOCKED.value
    assert progress["status"] == QuestStatus.ACTIVE.value


def test_speaking_to_ghost_before_first_objective_does_not_skip_progression() -> None:
    state = _fresh_state()
    snapshot = json.loads(json.dumps(state))

    result = apply_story_signal(state, NpcSpokenToSignal(npc_id="library_ghost"))

    assert result.changed is False
    assert result.outcome is StoryProgressionOutcome.NOT_APPLICABLE
    assert state == snapshot


def test_speaking_to_ghost_after_first_objective_advances_second_objective() -> None:
    state = _fresh_state()
    apply_story_signal(state, RoomEnteredSignal(room_id="library"))

    result = apply_story_signal(state, NpcSpokenToSignal(npc_id="library_ghost"))

    assert result.changed is True
    assert result.outcome is StoryProgressionOutcome.OBJECTIVE_ADVANCED
    assert result.objective_id == "speak_to_library_ghost"

    progress = state["story"]["quests"][QUEST_ID]
    assert progress["objectives"]["speak_to_library_ghost"] == ObjectiveStatus.COMPLETED.value
    assert progress["objectives"]["acquire_old_book"] == ObjectiveStatus.ACTIVE.value


def test_acquiring_old_book_completes_final_objective_and_quest_after_prerequisites() -> None:
    state = _fresh_state()
    apply_story_signal(state, RoomEnteredSignal(room_id="library"))
    apply_story_signal(state, NpcSpokenToSignal(npc_id="library_ghost"))

    result = apply_story_signal(state, ItemAcquiredSignal(item_id="old_book"))

    assert result.changed is True
    assert result.outcome is StoryProgressionOutcome.QUEST_COMPLETED
    assert result.objective_id == "acquire_old_book"
    assert result.new_quest_status is QuestStatus.COMPLETED

    progress = state["story"]["quests"][QUEST_ID]
    assert progress["status"] == QuestStatus.COMPLETED.value
    assert all(
        status == ObjectiveStatus.COMPLETED.value
        for status in progress["objectives"].values()
    )


def test_acquiring_old_book_too_early_does_not_complete_quest() -> None:
    state = _fresh_state()
    snapshot = json.loads(json.dumps(state))

    result = apply_story_signal(state, ItemAcquiredSignal(item_id="old_book"))

    assert result.changed is False
    assert result.outcome is StoryProgressionOutcome.NOT_APPLICABLE
    assert state == snapshot


def test_duplicate_signals_are_idempotent_and_do_not_double_advance() -> None:
    state = _fresh_state()
    apply_story_signal(state, RoomEnteredSignal(room_id="library"))
    snapshot = json.loads(json.dumps(state["story"]))

    result = apply_story_signal(state, RoomEnteredSignal(room_id="library"))

    assert result.changed is False
    assert result.outcome is StoryProgressionOutcome.ALREADY_SATISFIED
    assert state["story"] == snapshot


def test_duplicate_signal_after_quest_completion_is_idempotent() -> None:
    state = _fresh_state()
    apply_story_signal(state, RoomEnteredSignal(room_id="library"))
    apply_story_signal(state, NpcSpokenToSignal(npc_id="library_ghost"))
    apply_story_signal(state, ItemAcquiredSignal(item_id="old_book"))
    snapshot = json.loads(json.dumps(state["story"]))

    result = apply_story_signal(state, ItemAcquiredSignal(item_id="old_book"))

    assert result.changed is False
    assert result.outcome is StoryProgressionOutcome.ALREADY_SATISFIED
    assert state["story"] == snapshot


def test_unrelated_room_item_npc_signals_do_not_mutate_story_progress() -> None:
    state = _fresh_state()
    ensure_story_state(state)
    snapshot = json.loads(json.dumps(state["story"]))

    unrelated_signals = [
        RoomEnteredSignal(room_id="entry_hall"),
        NpcSpokenToSignal(npc_id="old_caretaker"),
        ItemAcquiredSignal(item_id="brass_key"),
        FactRecordedSignal(fact="cellar_door_unlocked"),
    ]
    for signal in unrelated_signals:
        result = apply_story_signal(state, signal)
        assert result.changed is False
        assert result.outcome is StoryProgressionOutcome.NOT_APPLICABLE

    assert state["story"] == snapshot


def test_malformed_story_progress_cannot_accidentally_grant_completion() -> None:
    state = _fresh_state()
    state["story"] = {
        "quests": {
            QUEST_ID: {
                "status": "completed",
                "objectives": {
                    "enter_library": "locked",
                    "speak_to_library_ghost": "completed",
                    "acquire_old_book": "locked",
                },
            }
        }
    }

    story = ensure_story_state(state)

    progress = story["quests"][QUEST_ID]
    assert progress["status"] == QuestStatus.ACTIVE.value
    assert progress["objectives"] == {
        "enter_library": ObjectiveStatus.ACTIVE.value,
        "speak_to_library_ghost": ObjectiveStatus.LOCKED.value,
        "acquire_old_book": ObjectiveStatus.LOCKED.value,
    }


def test_malformed_story_progress_with_inconsistent_status_resets_safely() -> None:
    state = _fresh_state()
    state["story"] = {
        "quests": {
            QUEST_ID: {
                "status": "active",
                "objectives": {
                    "enter_library": "completed",
                    "speak_to_library_ghost": "completed",
                    "acquire_old_book": "completed",
                },
            }
        }
    }

    story = ensure_story_state(state)

    progress = story["quests"][QUEST_ID]
    assert progress["status"] == QuestStatus.ACTIVE.value
    assert progress["objectives"]["enter_library"] == ObjectiveStatus.ACTIVE.value


@pytest.mark.parametrize("malformed_status", [[], {}])
def test_malformed_story_progress_with_unhashable_status_resets_safely(
    malformed_status: object,
) -> None:
    state = _fresh_state()
    state["story"] = {
        "quests": {
            QUEST_ID: {
                "status": malformed_status,
                "objectives": {
                    "enter_library": "active",
                    "speak_to_library_ghost": "locked",
                    "acquire_old_book": "locked",
                },
            }
        }
    }

    story = ensure_story_state(state)

    progress = story["quests"][QUEST_ID]
    assert progress["status"] == QuestStatus.ACTIVE.value
    assert progress["objectives"] == {
        "enter_library": ObjectiveStatus.ACTIVE.value,
        "speak_to_library_ghost": ObjectiveStatus.LOCKED.value,
        "acquire_old_book": ObjectiveStatus.LOCKED.value,
    }


def test_invalid_signal_payload_is_rejected_without_mutation() -> None:
    state = _fresh_state()
    ensure_story_state(state)
    snapshot = json.loads(json.dumps(state["story"]))

    result = apply_story_signal(state, {"signal_type": "not_a_real_signal"})

    assert result.changed is False
    assert result.outcome is StoryProgressionOutcome.INVALID_SIGNAL
    assert state["story"] == snapshot


def test_invalid_signal_without_story_namespace_leaves_entire_state_unchanged() -> None:
    state = _fresh_state()
    snapshot = json.loads(json.dumps(state))

    result = apply_story_signal(state, {"signal_type": "not_a_real_signal"})

    assert result.changed is False
    assert result.outcome is StoryProgressionOutcome.INVALID_SIGNAL
    assert state == snapshot


def test_canonical_dev_definitions_validate_successfully() -> None:
    validate_story_definitions(STORY_QUESTS)


def test_duplicate_objective_ids_within_quest_rejected() -> None:
    quest = QuestDefinition(
        id="duplicate_ids",
        title="Duplicate IDs",
        description="Test",
        objectives=(
            ObjectiveDefinition(
                id="same_id",
                order=0,
                description="First",
                signal_type=StorySignalType.ROOM_ENTERED,
                match_value="room_a",
            ),
            ObjectiveDefinition(
                id="same_id",
                order=1,
                description="Second",
                signal_type=StorySignalType.ROOM_ENTERED,
                match_value="room_b",
            ),
        ),
    )

    with pytest.raises(ValueError, match="Duplicate objective ID 'same_id'"):
        validate_story_definitions({"duplicate_ids": quest})


def test_duplicate_objective_orders_within_quest_rejected() -> None:
    quest = QuestDefinition(
        id="duplicate_orders",
        title="Duplicate Orders",
        description="Test",
        objectives=(
            ObjectiveDefinition(
                id="first",
                order=0,
                description="First",
                signal_type=StorySignalType.ROOM_ENTERED,
                match_value="room_a",
            ),
            ObjectiveDefinition(
                id="second",
                order=0,
                description="Second",
                signal_type=StorySignalType.ROOM_ENTERED,
                match_value="room_b",
            ),
        ),
    )

    with pytest.raises(ValueError, match="Duplicate objective order 0"):
        validate_story_definitions({"duplicate_orders": quest})


def test_non_contiguous_objective_orders_within_quest_rejected() -> None:
    quest = QuestDefinition(
        id="missing_order",
        title="Missing Order",
        description="Test",
        objectives=(
            ObjectiveDefinition(
                id="first",
                order=0,
                description="First",
                signal_type=StorySignalType.ROOM_ENTERED,
                match_value="room_a",
            ),
            ObjectiveDefinition(
                id="third",
                order=2,
                description="Third",
                signal_type=StorySignalType.ROOM_ENTERED,
                match_value="room_b",
            ),
        ),
    )

    with pytest.raises(ValueError, match="expected order 1"):
        validate_story_definitions({"missing_order": quest})


def test_out_of_order_objective_definition_tuple_rejected() -> None:
    quest = QuestDefinition(
        id="out_of_order",
        title="Out of Order",
        description="Test",
        objectives=(
            ObjectiveDefinition(
                id="second",
                order=1,
                description="Second",
                signal_type=StorySignalType.ROOM_ENTERED,
                match_value="room_b",
            ),
            ObjectiveDefinition(
                id="first",
                order=0,
                description="First",
                signal_type=StorySignalType.ROOM_ENTERED,
                match_value="room_a",
            ),
        ),
    )

    with pytest.raises(ValueError, match="position 0 has order 1"):
        validate_story_definitions({"out_of_order": quest})


def test_duplicate_condition_within_one_quest_rejected() -> None:
    quest = QuestDefinition(
        id="dup_quest",
        title="Duplicate Quest",
        description="Test",
        objectives=(
            ObjectiveDefinition(
                id="obj1",
                order=0,
                description="First",
                signal_type=StorySignalType.ROOM_ENTERED,
                match_value="library",
            ),
            ObjectiveDefinition(
                id="obj2",
                order=1,
                description="Second",
                signal_type=StorySignalType.ROOM_ENTERED,
                match_value="library",
            ),
        ),
    )
    with pytest.raises(ValueError, match="Duplicate story progression condition"):
        validate_story_definitions({"dup_quest": quest})


def test_duplicate_condition_across_two_quests_rejected() -> None:
    q1 = QuestDefinition(
        id="q1",
        title="Q1",
        description="Test",
        objectives=(
            ObjectiveDefinition(
                id="q1_obj",
                order=0,
                description="First",
                signal_type=StorySignalType.ROOM_ENTERED,
                match_value="shared_room",
            ),
        ),
    )
    q2 = QuestDefinition(
        id="q2",
        title="Q2",
        description="Test",
        objectives=(
            ObjectiveDefinition(
                id="q2_obj",
                order=0,
                description="Second",
                signal_type=StorySignalType.ROOM_ENTERED,
                match_value="shared_room",
            ),
        ),
    )
    with pytest.raises(ValueError, match="Duplicate story progression condition"):
        validate_story_definitions({"q1": q1, "q2": q2})


def test_different_conditions_across_multiple_quests_valid() -> None:
    q1 = QuestDefinition(
        id="q1",
        title="Q1",
        description="Test",
        objectives=(
            ObjectiveDefinition(
                id="q1_obj",
                order=0,
                description="First",
                signal_type=StorySignalType.ROOM_ENTERED,
                match_value="room_a",
            ),
        ),
    )
    q2 = QuestDefinition(
        id="q2",
        title="Q2",
        description="Test",
        objectives=(
            ObjectiveDefinition(
                id="q2_obj",
                order=0,
                description="Second",
                signal_type=StorySignalType.ROOM_ENTERED,
                match_value="room_b",
            ),
        ),
    )
    validate_story_definitions({"q1": q1, "q2": q2})


def test_stuck_completed_prefix_locked_suffix_without_active_objective_resets_safely() -> None:
    state = _fresh_state()
    state["story"] = {
        "quests": {
            QUEST_ID: {
                "status": "active",
                "objectives": {
                    "enter_library": "completed",
                    "speak_to_library_ghost": "locked",
                    "acquire_old_book": "locked",
                },
            }
        }
    }

    story = ensure_story_state(state)

    progress = story["quests"][QUEST_ID]
    assert progress["status"] == QuestStatus.ACTIVE.value
    assert progress["objectives"] == {
        "enter_library": ObjectiveStatus.ACTIVE.value,
        "speak_to_library_ghost": ObjectiveStatus.LOCKED.value,
        "acquire_old_book": ObjectiveStatus.LOCKED.value,
    }


def test_stuck_completed_completed_locked_without_active_objective_resets_safely() -> None:
    state = _fresh_state()
    state["story"] = {
        "quests": {
            QUEST_ID: {
                "status": "active",
                "objectives": {
                    "enter_library": "completed",
                    "speak_to_library_ghost": "completed",
                    "acquire_old_book": "locked",
                },
            }
        }
    }

    story = ensure_story_state(state)

    progress = story["quests"][QUEST_ID]
    assert progress["status"] == QuestStatus.ACTIVE.value
    assert progress["objectives"]["enter_library"] == ObjectiveStatus.ACTIVE.value


def test_all_locked_nonempty_quest_progress_resets_safely() -> None:
    state = _fresh_state()
    state["story"] = {
        "quests": {
            QUEST_ID: {
                "status": "inactive",
                "objectives": {
                    "enter_library": "locked",
                    "speak_to_library_ghost": "locked",
                    "acquire_old_book": "locked",
                },
            }
        }
    }

    story = ensure_story_state(state)

    assert story["quests"][QUEST_ID] == {
        "status": QuestStatus.ACTIVE.value,
        "objectives": {
            "enter_library": ObjectiveStatus.ACTIVE.value,
            "speak_to_library_ghost": ObjectiveStatus.LOCKED.value,
            "acquire_old_book": ObjectiveStatus.LOCKED.value,
        },
    }


def test_extra_persisted_objective_key_resets_known_quest_progress_safely() -> None:
    state = _fresh_state()
    state["story"] = {
        "quests": {
            QUEST_ID: {
                "status": "completed",
                "objectives": {
                    "enter_library": "completed",
                    "speak_to_library_ghost": "completed",
                    "acquire_old_book": "completed",
                    "unknown_objective": "completed",
                },
            }
        }
    }

    story = ensure_story_state(state)

    assert story["quests"][QUEST_ID]["status"] == QuestStatus.ACTIVE.value
    assert story["quests"][QUEST_ID]["objectives"] == {
        "enter_library": ObjectiveStatus.ACTIVE.value,
        "speak_to_library_ghost": ObjectiveStatus.LOCKED.value,
        "acquire_old_book": ObjectiveStatus.LOCKED.value,
    }


def test_unknown_persisted_quest_is_preserved_while_known_quest_normalizes() -> None:
    unknown_progress = {
        "status": "future_state",
        "objectives": {"unrecognized_objective": {"opaque": True}},
    }
    state = _fresh_state()
    state["story"] = {
        "quests": {
            QUEST_ID: {
                "status": "active",
                "objectives": {
                    "enter_library": "completed",
                    "speak_to_library_ghost": "active",
                    "acquire_old_book": "locked",
                },
            },
            "future_quest": unknown_progress,
        }
    }

    story = ensure_story_state(state)

    assert story["quests"]["future_quest"] is unknown_progress
    assert story["quests"][QUEST_ID] == {
        "status": QuestStatus.ACTIVE.value,
        "objectives": {
            "enter_library": ObjectiveStatus.COMPLETED.value,
            "speak_to_library_ghost": ObjectiveStatus.ACTIVE.value,
            "acquire_old_book": ObjectiveStatus.LOCKED.value,
        },
    }


def test_story_state_json_round_trip_preserves_progress() -> None:
    state = _fresh_state()
    apply_story_signal(state, RoomEnteredSignal(room_id="library"))

    encoded = json.dumps(state)
    reloaded = json.loads(encoded)

    story = ensure_story_state(reloaded)
    assert story["quests"][QUEST_ID]["objectives"]["enter_library"] == (
        ObjectiveStatus.COMPLETED.value
    )
    assert story["quests"][QUEST_ID]["objectives"]["speak_to_library_ghost"] == (
        ObjectiveStatus.ACTIVE.value
    )


def test_applying_story_signals_does_not_mutate_unrelated_state() -> None:
    state = _fresh_state()
    state["player"]["inventory"] = ["brass_key"]
    state["items"] = {"old_book": {"location": "room:library"}}
    state["npcs"] = {"library_ghost": {"location": "library", "status": "active"}}
    state["clock"] = {"tick": 3}
    state["facts"] = ["cellar_door_unlocked"]

    apply_story_signal(state, RoomEnteredSignal(room_id="library"))
    apply_story_signal(state, NpcSpokenToSignal(npc_id="library_ghost"))
    apply_story_signal(state, ItemAcquiredSignal(item_id="old_book"))

    assert state["player"] == {"location": "entry_hall", "inventory": ["brass_key"]}
    assert state["items"] == {"old_book": {"location": "room:library"}}
    assert state["npcs"] == {"library_ghost": {"location": "library", "status": "active"}}
    assert state["clock"] == {"tick": 3}
    assert state["facts"] == ["cellar_door_unlocked"]
