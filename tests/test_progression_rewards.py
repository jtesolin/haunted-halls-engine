from copy import deepcopy
from typing import Any

from app.game.abilities import evaluate_ability_availability
from app.game.campaign_state import build_fresh_campaign_state
from app.game.character_progression import (
    MAX_TRACK_POINTS,
    default_character_progression_state,
)
from app.game.progression_rewards import (
    AUTHORED_QUEST_COMPLETION_REWARDS,
    REWARD_CLAIMS_KEY,
    ProgressionRewardOutcome,
    apply_quest_completion_rewards,
    read_reward_claims,
    validate_progression_reward_definitions,
)
from app.schemas.abilities import AbilityAvailabilityStatus
from app.schemas.story import (
    ObjectiveStatus,
    QuestStatus,
    StoryProgressionOutcome,
    StoryProgressionResult,
)


def _completed_library_whisper() -> StoryProgressionResult:
    return StoryProgressionResult(
        changed=True,
        outcome=StoryProgressionOutcome.QUEST_COMPLETED,
        reason="completed",
        quest_id="librarys_whisper",
        objective_id="acquire_old_book",
        previous_quest_status=QuestStatus.ACTIVE,
        new_quest_status=QuestStatus.COMPLETED,
        previous_objective_status=ObjectiveStatus.ACTIVE,
        new_objective_status=ObjectiveStatus.COMPLETED,
    )


def _state_with_library_whisper_completed() -> dict[str, Any]:
    """Campaign state whose authoritative story snapshot already proves
    `librarys_whisper` completed via its canonical final objective, matching
    what a real `apply_story_signal` call would have just persisted."""
    state = build_fresh_campaign_state()
    state["story"] = {
        "quests": {
            "librarys_whisper": {
                "status": "completed",
                "objectives": {
                    "enter_library": "completed",
                    "speak_to_library_ghost": "completed",
                    "acquire_old_book": "completed",
                },
            }
        }
    }
    return state


def test_authored_reward_definitions_are_valid_and_canonical() -> None:
    definitions = validate_progression_reward_definitions()

    assert definitions == AUTHORED_QUEST_COMPLETION_REWARDS
    assert definitions[0].reward_id == "librarys_whisper_completion"
    assert definitions[0].progression_grants[0].amount == 2
    assert definitions[0].ability_unlock_ids == ("keen_eye",)


def test_legacy_and_read_claims_are_non_mutating() -> None:
    state = build_fresh_campaign_state()
    before = deepcopy(state)

    assert read_reward_claims(state) == (True, ())
    assert state == before


def test_valid_player_without_reward_namespace_is_legacy_no_claims() -> None:
    """A valid `player` dict simply lacking the reward-claims namespace is a
    legacy campaign with no claims, not malformed state."""
    state = build_fresh_campaign_state()
    assert REWARD_CLAIMS_KEY not in state["player"]

    assert read_reward_claims(state) == (True, ())


def test_present_malformed_player_is_read_as_invalid_not_legacy() -> None:
    for malformed_player in ("not-a-dict", ["also", "not", "a", "dict"], 42, None):
        state = build_fresh_campaign_state()
        state["player"] = malformed_player

        assert read_reward_claims(state) == (False, ())


def test_present_malformed_player_reward_application_fails_without_mutation() -> None:
    state = _state_with_library_whisper_completed()
    state["player"] = "not-a-dict"
    before = deepcopy(state)

    result = apply_quest_completion_rewards(state, _completed_library_whisper())

    assert result.outcome == ProgressionRewardOutcome.FAILED
    assert result.changed is False
    assert result.narrator_reward is None
    assert state == before
    assert state["player"] == "not-a-dict"


def test_malformed_claims_fail_safely_without_partial_reward() -> None:
    state = _state_with_library_whisper_completed()
    state["player"]["progression_rewards"] = {"claimed_reward_ids": ["keen_eye", 3]}
    before = deepcopy(state)

    result = apply_quest_completion_rewards(state, _completed_library_whisper())

    assert result.changed is False
    assert result.outcome == ProgressionRewardOutcome.FAILED
    assert state == before


def test_library_completion_grants_and_claims_once() -> None:
    state = _state_with_library_whisper_completed()

    first = apply_quest_completion_rewards(state, _completed_library_whisper())
    second = apply_quest_completion_rewards(state, _completed_library_whisper())

    assert first.changed is True
    assert first.outcome == ProgressionRewardOutcome.APPLIED
    assert first.narrator_reward is not None
    assert first.narrator_reward.progression_grants[0].new_points == 2
    assert first.narrator_reward.unlocked_abilities[0].display_name == "Keen Eye"
    assert state["player"]["progression"]["tracks"]["investigation"] == 2
    assert state["player"]["progression"]["unlocked_abilities"] == ["keen_eye"]
    assert state["player"]["progression_rewards"]["claimed_reward_ids"] == [
        "librarys_whisper_completion"
    ]
    assert second.changed is False
    assert second.outcome == ProgressionRewardOutcome.ALREADY_CLAIMED
    assert second.narrator_reward is None
    assert evaluate_ability_availability(state, "keen_eye").status == AbilityAvailabilityStatus.AVAILABLE


def test_non_completion_does_not_mutate_story_or_progression() -> None:
    state = build_fresh_campaign_state()
    story_before = deepcopy(state.get("story"))
    result = apply_quest_completion_rewards(
        state,
        StoryProgressionResult(
            changed=True,
            outcome=StoryProgressionOutcome.OBJECTIVE_ADVANCED,
            reason="not final",
            quest_id="librarys_whisper",
        ),
    )

    assert result.outcome == ProgressionRewardOutcome.NOT_APPLICABLE
    assert state.get("story") == story_before
    assert "progression" not in state["player"]


def test_preowned_ability_is_not_projected_as_newly_unlocked() -> None:
    state = _state_with_library_whisper_completed()
    state["player"]["progression"] = default_character_progression_state()
    state["player"]["progression"]["unlocked_abilities"] = ["keen_eye"]

    result = apply_quest_completion_rewards(state, _completed_library_whisper())

    assert result.outcome == ProgressionRewardOutcome.APPLIED
    assert result.narrator_reward is not None
    assert result.narrator_reward.unlocked_abilities == []
    assert state["player"]["progression"]["unlocked_abilities"] == ["keen_eye"]
    assert state["player"]["progression_rewards"]["claimed_reward_ids"] == [
        "librarys_whisper_completion"
    ]


def test_fully_no_op_reward_still_claims_but_reports_no_narrator_reward() -> None:
    """When the authored reward's grant and unlock are both already at their
    end state (capped track, pre-owned ability) but the reward has not yet
    been claimed, the canonical claim must still be recorded and the outcome
    must remain `APPLIED`, but there is no authoritative player-facing change
    to narrate, so `narrator_reward` must be `None`."""
    state = _state_with_library_whisper_completed()
    progression = default_character_progression_state()
    progression["tracks"]["investigation"] = MAX_TRACK_POINTS
    progression["unlocked_abilities"] = ["keen_eye"]
    state["player"]["progression"] = progression

    result = apply_quest_completion_rewards(state, _completed_library_whisper())

    assert result.outcome == ProgressionRewardOutcome.APPLIED
    assert result.changed is True
    assert result.narrator_reward is None
    assert state["player"]["progression"]["tracks"]["investigation"] == MAX_TRACK_POINTS
    assert state["player"]["progression"]["unlocked_abilities"] == ["keen_eye"]
    assert state["player"]["progression_rewards"]["claimed_reward_ids"] == [
        "librarys_whisper_completion"
    ]


def test_missing_transition_fields_are_not_eligible_for_a_reward() -> None:
    """A completion result lacking the objective-transition fields cannot
    prove the canonical first completion and must not be rewarded."""
    state = _state_with_library_whisper_completed()
    before = deepcopy(state)
    inconsistent_result = StoryProgressionResult(
        changed=True,
        outcome=StoryProgressionOutcome.QUEST_COMPLETED,
        reason="completed",
        quest_id="librarys_whisper",
        objective_id="acquire_old_book",
        previous_quest_status=QuestStatus.ACTIVE,
        new_quest_status=QuestStatus.COMPLETED,
        # previous_objective_status / new_objective_status intentionally omitted.
    )

    result = apply_quest_completion_rewards(state, inconsistent_result)

    assert result.outcome == ProgressionRewardOutcome.NOT_APPLICABLE
    assert result.changed is False
    assert result.narrator_reward is None
    assert state == before


def test_previous_quest_already_completed_is_not_eligible_for_a_reward() -> None:
    """A result claiming completion from an already-`COMPLETED` quest status
    cannot prove the first ACTIVE -> COMPLETED transition."""
    state = _state_with_library_whisper_completed()
    before = deepcopy(state)
    inconsistent_result = StoryProgressionResult(
        changed=True,
        outcome=StoryProgressionOutcome.QUEST_COMPLETED,
        reason="completed",
        quest_id="librarys_whisper",
        objective_id="acquire_old_book",
        previous_quest_status=QuestStatus.COMPLETED,
        new_quest_status=QuestStatus.COMPLETED,
        previous_objective_status=ObjectiveStatus.ACTIVE,
        new_objective_status=ObjectiveStatus.COMPLETED,
    )

    result = apply_quest_completion_rewards(state, inconsistent_result)

    assert result.outcome == ProgressionRewardOutcome.NOT_APPLICABLE
    assert result.changed is False
    assert state == before


def test_wrong_final_objective_is_not_eligible_for_a_reward() -> None:
    """A completion claim naming a non-final objective cannot prove the
    canonical final-objective transition, even if quest status looks right."""
    state = _state_with_library_whisper_completed()
    before = deepcopy(state)
    inconsistent_result = StoryProgressionResult(
        changed=True,
        outcome=StoryProgressionOutcome.QUEST_COMPLETED,
        reason="completed",
        quest_id="librarys_whisper",
        objective_id="enter_library",
        previous_quest_status=QuestStatus.ACTIVE,
        new_quest_status=QuestStatus.COMPLETED,
        previous_objective_status=ObjectiveStatus.ACTIVE,
        new_objective_status=ObjectiveStatus.COMPLETED,
    )

    result = apply_quest_completion_rewards(state, inconsistent_result)

    assert result.outcome == ProgressionRewardOutcome.NOT_APPLICABLE
    assert result.changed is False
    assert state == before


def test_inconsistent_objective_transition_is_not_eligible_for_a_reward() -> None:
    """A completion claim whose objective transition is not a real
    ACTIVE -> COMPLETED move must not be treated as a genuine completion."""
    state = _state_with_library_whisper_completed()
    before = deepcopy(state)
    inconsistent_result = StoryProgressionResult(
        changed=True,
        outcome=StoryProgressionOutcome.QUEST_COMPLETED,
        reason="completed",
        quest_id="librarys_whisper",
        objective_id="acquire_old_book",
        previous_quest_status=QuestStatus.ACTIVE,
        new_quest_status=QuestStatus.COMPLETED,
        previous_objective_status=ObjectiveStatus.LOCKED,
        new_objective_status=ObjectiveStatus.COMPLETED,
    )

    result = apply_quest_completion_rewards(state, inconsistent_result)

    assert result.outcome == ProgressionRewardOutcome.NOT_APPLICABLE
    assert result.changed is False
    assert state == before


def test_capped_progression_track_excludes_fake_narrator_increase() -> None:
    """`grant_progress` can report `changed=True` from unrelated progression
    normalization alone, even when the rewarded track is already capped and
    does not actually increase. The Narrator must not be told about a
    non-existent point increase, but the reward is still claimed."""
    state = _state_with_library_whisper_completed()
    progression = default_character_progression_state()
    progression["tracks"]["investigation"] = MAX_TRACK_POINTS
    progression["stray_legacy_field"] = "ignored-by-normalization"
    state["player"]["progression"] = progression

    result = apply_quest_completion_rewards(state, _completed_library_whisper())

    assert result.outcome == ProgressionRewardOutcome.APPLIED
    assert result.narrator_reward is not None
    assert result.narrator_reward.progression_grants == []
    assert [ability.ability_id for ability in result.narrator_reward.unlocked_abilities] == [
        "keen_eye"
    ]
    assert state["player"]["progression"]["tracks"]["investigation"] == MAX_TRACK_POINTS
    assert "stray_legacy_field" not in state["player"]["progression"]
    assert state["player"]["progression_rewards"]["claimed_reward_ids"] == [
        "librarys_whisper_completion"
    ]
