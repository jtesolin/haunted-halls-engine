from copy import deepcopy

from app.game.abilities import evaluate_ability_availability
from app.game.campaign_state import build_fresh_campaign_state
from app.game.progression_rewards import (
    AUTHORED_QUEST_COMPLETION_REWARDS,
    apply_quest_completion_rewards,
    read_reward_claims,
    validate_progression_reward_definitions,
)
from app.schemas.abilities import AbilityAvailabilityStatus
from app.schemas.story import (
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
    )


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


def test_malformed_claims_fail_safely_without_partial_reward() -> None:
    state = build_fresh_campaign_state()
    state["player"]["progression_rewards"] = {"claimed_reward_ids": ["keen_eye", 3]}
    before = deepcopy(state)

    result = apply_quest_completion_rewards(state, _completed_library_whisper())

    assert result.changed is False
    assert result.applicable is True
    assert state == before


def test_library_completion_grants_and_claims_once() -> None:
    state = build_fresh_campaign_state()

    first = apply_quest_completion_rewards(state, _completed_library_whisper())
    second = apply_quest_completion_rewards(state, _completed_library_whisper())

    assert first.changed is True
    assert first.narrator_reward is not None
    assert first.narrator_reward.progression_grants[0].new_points == 2
    assert first.narrator_reward.unlocked_abilities[0].display_name == "Keen Eye"
    assert state["player"]["progression"]["tracks"]["investigation"] == 2
    assert state["player"]["progression"]["unlocked_abilities"] == ["keen_eye"]
    assert state["player"]["progression_rewards"]["claimed_reward_ids"] == [
        "librarys_whisper_completion"
    ]
    assert second.changed is False
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

    assert result.applicable is False
    assert state.get("story") == story_before
    assert "progression" not in state["player"]
