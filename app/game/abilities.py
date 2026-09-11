from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from app.game.character_progression import (
    MAX_TRACK_POINTS,
    MIN_TRACK_POINTS,
    read_character_progression_state,
)
from app.schemas.abilities import (
    AbilityAvailabilityResult,
    AbilityAvailabilityStatus,
    AbilityCheckOutcome,
    AbilityCheckResult,
)
from app.schemas.character_progression import ProgressionTrackId

MIN_CHECK_DIFFICULTY = MIN_TRACK_POINTS
MAX_CHECK_DIFFICULTY = MAX_TRACK_POINTS


@dataclass(frozen=True)
class AbilityDefinition:
    """Immutable static ability definition used by authoritative 8C rules."""

    ability_id: str
    display_name: str
    short_description: str
    track: ProgressionTrackId
    minimum_points: int


CANONICAL_ABILITY_DEFINITIONS: tuple[AbilityDefinition, ...] = (
    AbilityDefinition(
        ability_id="keen_eye",
        display_name="Keen Eye",
        short_description="notice subtle environmental evidence",
        track=ProgressionTrackId.INVESTIGATION,
        minimum_points=2,
    ),
    AbilityDefinition(
        ability_id="steady_nerves",
        display_name="Steady Nerves",
        short_description="remain composed under frightening pressure",
        track=ProgressionTrackId.RESOLVE,
        minimum_points=2,
    ),
    AbilityDefinition(
        ability_id="read_the_room",
        display_name="Read the Room",
        short_description="interpret social cues and NPC behavior",
        track=ProgressionTrackId.RAPPORT,
        minimum_points=2,
    ),
    AbilityDefinition(
        ability_id="occult_insight",
        display_name="Occult Insight",
        short_description="recognize supernatural patterns or meaning",
        track=ProgressionTrackId.OCCULT,
        minimum_points=2,
    ),
)


def validate_ability_definitions(
    definitions: Sequence[AbilityDefinition],
) -> tuple[AbilityDefinition, ...]:
    """Validate the static ability-definition set and return it unchanged."""
    validated: list[AbilityDefinition] = []
    seen: set[str] = set()
    for index, definition in enumerate(definitions):
        if not isinstance(definition, AbilityDefinition):
            raise ValueError(f"Ability definition at index {index} is not an AbilityDefinition.")
        if not isinstance(definition.ability_id, str) or not definition.ability_id:
            raise ValueError(f"Ability definition at index {index} has an invalid ability_id.")
        if definition.ability_id.strip() != definition.ability_id:
            raise ValueError(
                f"Ability definition at index {index} has an ability_id with leading or "
                f"trailing whitespace: {definition.ability_id!r}."
            )
        if definition.ability_id in seen:
            raise ValueError(f"Duplicate ability_id '{definition.ability_id}' in ability definitions.")
        if not isinstance(definition.track, ProgressionTrackId):
            raise ValueError(
                f"Ability '{definition.ability_id}' has an invalid track reference: "
                f"{definition.track!r}."
            )
        if isinstance(definition.minimum_points, bool) or not isinstance(definition.minimum_points, int):
            raise ValueError(
                f"Ability '{definition.ability_id}' minimum_points must be an integer, not "
                f"{type(definition.minimum_points).__name__}."
            )
        if not MIN_TRACK_POINTS <= definition.minimum_points <= MAX_TRACK_POINTS:
            raise ValueError(
                f"Ability '{definition.ability_id}' minimum_points {definition.minimum_points} is "
                f"outside {MIN_TRACK_POINTS}..{MAX_TRACK_POINTS}."
            )
        validated.append(definition)
        seen.add(definition.ability_id)
    return tuple(validated)


VALIDATED_ABILITY_DEFINITIONS = validate_ability_definitions(CANONICAL_ABILITY_DEFINITIONS)
_VALIDATED_ABILITY_REGISTRY = {
    definition.ability_id: definition for definition in VALIDATED_ABILITY_DEFINITIONS
}
ABILITY_REGISTRY: Mapping[str, AbilityDefinition] = MappingProxyType(_VALIDATED_ABILITY_REGISTRY)


def get_ability_definition(ability_id: str) -> AbilityDefinition | None:
    """Return the authoritative static ability definition for the given ID."""
    return ABILITY_REGISTRY.get(ability_id)


def evaluate_ability_availability(
    state: dict[str, Any], ability_id: str
) -> AbilityAvailabilityResult:
    """Evaluate ability availability from normalized progression without mutation."""
    definition = get_ability_definition(ability_id)
    if definition is None:
        return AbilityAvailabilityResult(
            ability_id=ability_id,
            available=False,
            owned=False,
            track_id=None,
            track_points=None,
            minimum_points=None,
            status=AbilityAvailabilityStatus.UNKNOWN_ABILITY,
            error_code="unknown_ability",
            reason=f"Ability '{ability_id}' is not a known 8C ability.",
        )

    progression = read_character_progression_state(state)
    track_id = definition.track
    track_points = progression["tracks"][track_id.value]
    owned = ability_id in progression["unlocked_abilities"]

    if not owned:
        return AbilityAvailabilityResult(
            ability_id=ability_id,
            available=False,
            owned=False,
            track_id=track_id,
            track_points=track_points,
            minimum_points=definition.minimum_points,
            status=AbilityAvailabilityStatus.LOCKED,
            error_code="not_owned",
            reason=f"Ability '{ability_id}' is not unlocked for the player.",
        )

    if track_points < definition.minimum_points:
        return AbilityAvailabilityResult(
            ability_id=ability_id,
            available=False,
            owned=True,
            track_id=track_id,
            track_points=track_points,
            minimum_points=definition.minimum_points,
            status=AbilityAvailabilityStatus.INSUFFICIENT_PROGRESSION,
            error_code="insufficient_progression",
            reason=(
                f"Ability '{ability_id}' requires {track_id.value} at least "
                f"{definition.minimum_points} points, but the player has {track_points}."
            ),
        )

    return AbilityAvailabilityResult(
        ability_id=ability_id,
        available=True,
        owned=True,
        track_id=track_id,
        track_points=track_points,
        minimum_points=definition.minimum_points,
        status=AbilityAvailabilityStatus.AVAILABLE,
        error_code=None,
        reason=None,
    )


def resolve_ability_check(
    state: dict[str, Any], ability_id: str, difficulty: int
) -> AbilityCheckResult:
    """Resolve a deterministic ability check from current normalized progression."""
    if isinstance(difficulty, bool) or not isinstance(difficulty, int):
        return AbilityCheckResult(
            ability_id=ability_id,
            outcome=AbilityCheckOutcome.INVALID_DIFFICULTY,
            resolved=False,
            success=None,
            track_id=None,
            track_points=None,
            difficulty=None,
            margin=None,
            error_code="invalid_difficulty",
            reason=(
                f"Difficulty must be an integer between {MIN_CHECK_DIFFICULTY} and "
                f"{MAX_CHECK_DIFFICULTY}."
            ),
        )
    if difficulty < MIN_CHECK_DIFFICULTY or difficulty > MAX_CHECK_DIFFICULTY:
        return AbilityCheckResult(
            ability_id=ability_id,
            outcome=AbilityCheckOutcome.INVALID_DIFFICULTY,
            resolved=False,
            success=None,
            track_id=None,
            track_points=None,
            difficulty=difficulty,
            margin=None,
            error_code="invalid_difficulty",
            reason=(
                f"Difficulty {difficulty} is outside the permitted range "
                f"{MIN_CHECK_DIFFICULTY}..{MAX_CHECK_DIFFICULTY}."
            ),
        )

    availability = evaluate_ability_availability(state, ability_id)
    if availability.status == AbilityAvailabilityStatus.UNKNOWN_ABILITY:
        return AbilityCheckResult(
            ability_id=ability_id,
            outcome=AbilityCheckOutcome.UNKNOWN_ABILITY,
            resolved=False,
            success=None,
            track_id=None,
            track_points=None,
            difficulty=difficulty,
            margin=None,
            error_code=availability.error_code,
            reason=availability.reason,
        )
    if availability.status in (
        AbilityAvailabilityStatus.LOCKED,
        AbilityAvailabilityStatus.INSUFFICIENT_PROGRESSION,
    ):
        return AbilityCheckResult(
            ability_id=ability_id,
            outcome=AbilityCheckOutcome.UNAVAILABLE,
            resolved=False,
            success=None,
            track_id=availability.track_id,
            track_points=availability.track_points,
            difficulty=difficulty,
            margin=None,
            error_code=availability.error_code,
            reason=availability.reason,
        )

    track_points = availability.track_points
    if track_points is None:
        return AbilityCheckResult(
            ability_id=ability_id,
            outcome=AbilityCheckOutcome.UNAVAILABLE,
            resolved=False,
            success=None,
            track_id=availability.track_id,
            track_points=None,
            difficulty=difficulty,
            margin=None,
            error_code="unavailable",
            reason="Ability state is not actionable.",
        )

    margin = track_points - difficulty
    if margin >= 0:
        return AbilityCheckResult(
            ability_id=ability_id,
            outcome=AbilityCheckOutcome.SUCCESS,
            resolved=True,
            success=True,
            track_id=availability.track_id,
            track_points=track_points,
            difficulty=difficulty,
            margin=margin,
            error_code=None,
            reason=None,
        )

    return AbilityCheckResult(
        ability_id=ability_id,
        outcome=AbilityCheckOutcome.FAILURE,
        resolved=True,
        success=False,
        track_id=availability.track_id,
        track_points=track_points,
        difficulty=difficulty,
        margin=margin,
        error_code=None,
        reason=None,
    )


__all__ = [
    "AbilityDefinition",
    "CANONICAL_ABILITY_DEFINITIONS",
    "MIN_CHECK_DIFFICULTY",
    "MAX_CHECK_DIFFICULTY",
    "ABILITY_REGISTRY",
    "VALIDATED_ABILITY_DEFINITIONS",
    "get_ability_definition",
    "validate_ability_definitions",
    "evaluate_ability_availability",
    "resolve_ability_check",
]
