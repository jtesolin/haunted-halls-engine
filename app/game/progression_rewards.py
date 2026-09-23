"""Deterministic authored quest-completion rewards.

Reward definitions are static content.  A story result can make one of them
eligible, but neither a model nor player text can select or grant a reward.
Claims live in campaign JSON because they are campaign-authoritative state,
not database schema.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from app.game.abilities import ABILITY_REGISTRY
from app.game.character_progression import (
    MAX_TRACK_POINTS,
    grant_progress,
    unlock_ability,
)
from app.game.story import STORY_QUESTS
from app.schemas.character_progression import (
    NarratorProgressionGrant,
    NarratorProgressionReward,
    NarratorUnlockedAbility,
    ProgressionTrackId,
)
from app.schemas.story import StoryProgressionOutcome, StoryProgressionResult

REWARD_CLAIMS_KEY = "progression_rewards"
CLAIMED_REWARD_IDS_KEY = "claimed_reward_ids"
MAX_REWARD_ID_LENGTH = 100


@dataclass(frozen=True)
class ProgressionGrantDefinition:
    track_id: ProgressionTrackId
    amount: int


@dataclass(frozen=True)
class QuestCompletionRewardDefinition:
    reward_id: str
    quest_id: str
    progression_grants: tuple[ProgressionGrantDefinition, ...] = ()
    ability_unlock_ids: tuple[str, ...] = ()


LIBRARYS_WHISPER_COMPLETION_REWARD = QuestCompletionRewardDefinition(
    reward_id="librarys_whisper_completion",
    quest_id="librarys_whisper",
    progression_grants=(
        ProgressionGrantDefinition(ProgressionTrackId.INVESTIGATION, 2),
    ),
    ability_unlock_ids=("keen_eye",),
)

AUTHORED_QUEST_COMPLETION_REWARDS: tuple[QuestCompletionRewardDefinition, ...] = (
    LIBRARYS_WHISPER_COMPLETION_REWARD,
)


def validate_progression_reward_definitions(
    definitions: tuple[QuestCompletionRewardDefinition, ...] = AUTHORED_QUEST_COMPLETION_REWARDS,
) -> tuple[QuestCompletionRewardDefinition, ...]:
    """Validate authored reward metadata and preserve its deterministic order."""
    seen: set[str] = set()
    for index, reward in enumerate(definitions):
        if not isinstance(reward, QuestCompletionRewardDefinition):
            raise ValueError(f"Reward definition at index {index} is invalid.")
        if (
            not reward.reward_id
            or len(reward.reward_id) > MAX_REWARD_ID_LENGTH
            or reward.reward_id.strip() != reward.reward_id
        ):
            raise ValueError(f"Reward definition at index {index} has an invalid reward_id.")
        if reward.reward_id in seen:
            raise ValueError(f"Duplicate reward_id '{reward.reward_id}'.")
        if reward.quest_id not in STORY_QUESTS:
            raise ValueError(f"Reward '{reward.reward_id}' references an unknown quest.")
        for grant in reward.progression_grants:
            if not isinstance(grant.track_id, ProgressionTrackId):
                raise ValueError(f"Reward '{reward.reward_id}' references an unknown track.")
            if (
                isinstance(grant.amount, bool)
                or not isinstance(grant.amount, int)
                or not 0 < grant.amount <= MAX_TRACK_POINTS
            ):
                raise ValueError(f"Reward '{reward.reward_id}' has an invalid grant amount.")
        if len(set(reward.ability_unlock_ids)) != len(reward.ability_unlock_ids):
            raise ValueError(f"Reward '{reward.reward_id}' repeats an ability.")
        for ability_id in reward.ability_unlock_ids:
            if ability_id not in ABILITY_REGISTRY:
                raise ValueError(
                    f"Reward '{reward.reward_id}' references unknown ability '{ability_id}'."
                )
        seen.add(reward.reward_id)
    return definitions


validate_progression_reward_definitions()


def read_reward_claims(state: dict[str, Any]) -> tuple[bool, tuple[str, ...]]:
    """Read claims without mutation; the boolean is false for malformed state."""
    player = state.get("player")
    if not isinstance(player, dict) or REWARD_CLAIMS_KEY not in player:
        return True, ()
    namespace = player[REWARD_CLAIMS_KEY]
    if not isinstance(namespace, dict):
        return False, ()
    raw_ids = namespace.get(CLAIMED_REWARD_IDS_KEY)
    if not isinstance(raw_ids, list) or any(
        not isinstance(reward_id, str) or not reward_id or reward_id.strip() != reward_id
        for reward_id in raw_ids
    ):
        return False, ()
    if len(set(raw_ids)) != len(raw_ids):
        return False, ()
    return True, tuple(raw_ids)


class ProgressionRewardOutcome(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    APPLIED = "applied"
    ALREADY_CLAIMED = "already_claimed"
    FAILED = "failed"


@dataclass(frozen=True)
class ProgressionRewardResult:
    outcome: ProgressionRewardOutcome
    changed: bool
    reward_id: str | None = None
    reason: str = ""
    narrator_reward: NarratorProgressionReward | None = None


def apply_quest_completion_rewards(
    state: dict[str, Any],
    story_result: StoryProgressionResult | None,
) -> ProgressionRewardResult:
    """Apply at most one newly completed authored quest reward atomically."""
    if (
        story_result is None
        or not story_result.changed
        or story_result.outcome != StoryProgressionOutcome.QUEST_COMPLETED
        or story_result.quest_id is None
    ):
        return ProgressionRewardResult(
            ProgressionRewardOutcome.NOT_APPLICABLE,
            False,
            reason="Story result is not a quest completion.",
        )

    reward = next(
        (item for item in AUTHORED_QUEST_COMPLETION_REWARDS if item.quest_id == story_result.quest_id),
        None,
    )
    if reward is None:
        return ProgressionRewardResult(
            ProgressionRewardOutcome.NOT_APPLICABLE,
            False,
            reason="No authored reward matches the quest.",
        )

    claims_valid, claimed_ids = read_reward_claims(state)
    if not claims_valid:
        return ProgressionRewardResult(
            ProgressionRewardOutcome.FAILED,
            False,
            reward.reward_id,
            "Reward claim state is malformed.",
        )
    if reward.reward_id in claimed_ids:
        return ProgressionRewardResult(
            ProgressionRewardOutcome.ALREADY_CLAIMED,
            False,
            reward.reward_id,
            "Reward has already been claimed.",
        )

    candidate = deepcopy(state)
    grant_results = [
        grant_progress(candidate, grant.track_id, grant.amount)
        for grant in reward.progression_grants
    ]
    unlock_results = [
        unlock_ability(candidate, ability_id) for ability_id in reward.ability_unlock_ids
    ]
    if any(not result.success for result in (*grant_results, *unlock_results)):
        return ProgressionRewardResult(
            ProgressionRewardOutcome.FAILED,
            False,
            reward.reward_id,
            "Authored reward could not be applied.",
        )

    candidate.setdefault("player", {})[REWARD_CLAIMS_KEY] = {
        CLAIMED_REWARD_IDS_KEY: [*claimed_ids, reward.reward_id]
    }
    state.clear()
    state.update(candidate)
    narrator_reward = NarratorProgressionReward(
        reward_id=reward.reward_id,
        progression_grants=[
            NarratorProgressionGrant(
                track_id=cast(ProgressionTrackId, result.track_id),
                prior_points=result.prior_points,
                new_points=result.new_points,
            )
            for result in grant_results
            if result.changed
        ],
        unlocked_abilities=[
            NarratorUnlockedAbility(
                ability_id=result.ability_id,
                display_name=ABILITY_REGISTRY[result.ability_id].display_name,
            )
            for result in unlock_results
            if result.changed and not result.already_unlocked
        ],
    )
    return ProgressionRewardResult(
        ProgressionRewardOutcome.APPLIED,
        True,
        reward.reward_id,
        "Authored reward applied.",
        narrator_reward,
    )
