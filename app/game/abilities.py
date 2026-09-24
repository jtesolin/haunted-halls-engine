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
from app.schemas.generated_abilities import (
    AbilityGameplayResult,
    AbilityGameplayStatus,
    AbilityDetail,
    AbilityDomain,
    AbilityEffect,
    GeneratedAbilityDefinition,
    GeneratedAbilityKind,
)

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


def generated_ability_definitions(state: dict[str, Any]) -> tuple[GeneratedAbilityDefinition, ...]:
    """Read persisted generated definitions without repairing legacy state.

    The missing field is the compatible legacy-campaign representation. Any
    present field must be a complete validated starter set, rather than being
    partially ignored or repaired during a later lookup.
    """
    player = state.get("player")
    raw_definitions = player.get("generated_abilities") if isinstance(player, dict) else None
    if raw_definitions is None:
        return ()
    if not isinstance(raw_definitions, list):
        raise ValueError("Persisted generated ability definitions must be a list.")
    definitions: list[GeneratedAbilityDefinition] = []
    for raw_definition in raw_definitions:
        try:
            definition = GeneratedAbilityDefinition.model_validate(raw_definition)
            validate_generated_ability_definition(definition)
        except (TypeError, ValueError) as exc:
            raise ValueError("Persisted generated ability definition is invalid.") from exc
        definitions.append(definition)
    return validate_starter_ability_definitions(definitions)


def validate_generated_ability_definition(definition: GeneratedAbilityDefinition) -> None:
    """Validate generated content against the small engine-owned vocabulary."""
    if definition.ability_id in ABILITY_REGISTRY:
        raise ValueError(f"Generated ability id '{definition.ability_id}' collides with a built-in ability.")
    if definition.kind == GeneratedAbilityKind.SENSORY:
        if definition.mechanics.effect != AbilityEffect.SENSE or definition.mechanics.domain != AbilityDomain.SURROUNDINGS:
            raise ValueError("Sensory abilities must use the surroundings sense mechanic.")
    elif definition.kind == GeneratedAbilityKind.UTILITY:
        if definition.mechanics.effect != AbilityEffect.MINOR_UTILITY or definition.mechanics.domain != AbilityDomain.OBJECT:
            raise ValueError("Utility abilities must use the object minor-utility mechanic.")
    if definition.mechanics.detail not in {AbilityDetail.LIMITED, AbilityDetail.PRACTICAL}:
        raise ValueError("Generated ability detail is not supported.")
    if any(requirement not in {"line_of_sight", "nearby"} for requirement in definition.mechanics.requires):
        raise ValueError("Generated ability uses an unsupported requirement.")
    if definition.mechanics.bypasses:
        raise ValueError("Generated starter abilities may not bypass core gameplay constraints.")


def validate_starter_ability_definitions(
    definitions: Sequence[GeneratedAbilityDefinition],
) -> tuple[GeneratedAbilityDefinition, GeneratedAbilityDefinition]:
    """Validate the exactly-two modest starter set before it reaches state."""
    if len(definitions) != 2:
        raise ValueError("Exactly two generated starter abilities are required.")
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    kinds: set[GeneratedAbilityKind] = set()
    for definition in definitions:
        validate_generated_ability_definition(definition)
        if definition.ability_id in seen_ids:
            raise ValueError("Generated starter ability ids must be distinct.")
        normalized_name = definition.display_name.casefold()
        if normalized_name in seen_names:
            raise ValueError("Generated starter ability names must be distinct.")
        seen_ids.add(definition.ability_id)
        seen_names.add(normalized_name)
        kinds.add(definition.kind)
    if kinds != {GeneratedAbilityKind.SENSORY, GeneratedAbilityKind.UTILITY}:
        raise ValueError("Starter abilities require one sensory and one utility definition.")
    return (definitions[0], definitions[1])


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


def _generated_definition_by_id(
    state: dict[str, Any], ability_id: str
) -> GeneratedAbilityDefinition | None:
    return next(
        (definition for definition in generated_ability_definitions(state) if definition.ability_id == ability_id),
        None,
    )


def evaluate_ability_availability(
    state: dict[str, Any], ability_id: str
) -> AbilityAvailabilityResult:
    """Evaluate ability availability from normalized progression without mutation."""
    definition = get_ability_definition(ability_id)
    generated_definition = _generated_definition_by_id(state, ability_id)
    if generated_definition is not None:
        track_id = generated_definition.track
        minimum_points = generated_definition.minimum_points
    elif definition is not None:
        track_id = definition.track
        minimum_points = definition.minimum_points
    else:
        track_id = None
        minimum_points = None
    if track_id is None or minimum_points is None:
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
    track_points = progression["tracks"][track_id.value]
    owned = ability_id in progression["unlocked_abilities"]

    if not owned:
        return AbilityAvailabilityResult(
            ability_id=ability_id,
            available=False,
            owned=False,
            track_id=track_id,
            track_points=track_points,
            minimum_points=minimum_points,
            status=AbilityAvailabilityStatus.LOCKED,
            error_code="not_owned",
            reason=f"Ability '{ability_id}' is not unlocked for the player.",
        )

    if track_points < minimum_points:
        return AbilityAvailabilityResult(
            ability_id=ability_id,
            available=False,
            owned=True,
            track_id=track_id,
            track_points=track_points,
            minimum_points=minimum_points,
            status=AbilityAvailabilityStatus.INSUFFICIENT_PROGRESSION,
            error_code="insufficient_progression",
            reason=(
                f"Ability '{ability_id}' requires {track_id.value} at least "
                f"{minimum_points} points, but the player has {track_points}."
            ),
        )

    return AbilityAvailabilityResult(
        ability_id=ability_id,
        available=True,
        owned=True,
        track_id=track_id,
        track_points=track_points,
        minimum_points=minimum_points,
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


def resolve_gameplay_ability_check(
    state: dict[str, Any], ability_id: str
) -> AbilityGameplayResult:
    """Resolve the small authored player-check vocabulary for the current state."""
    built_in = get_ability_definition(ability_id)
    generated = _generated_definition_by_id(state, ability_id)
    if built_in is None and generated is None:
        return AbilityGameplayResult(
            ability_id=ability_id,
            status=AbilityGameplayStatus.UNKNOWN_ABILITY,
            error_code="unknown_ability",
            reason="The requested ability is not known for this campaign.",
        )

    if built_in is not None:
        display_name = built_in.display_name
        description = built_in.short_description
    else:
        assert generated is not None
        display_name = generated.display_name
        description = generated.description
    availability = evaluate_ability_availability(state, ability_id)
    if not availability.available:
        return AbilityGameplayResult(
            ability_id=ability_id,
            display_name=display_name,
            description=description,
            available=False,
            status=AbilityGameplayStatus.UNAVAILABLE,
            error_code=availability.error_code,
            reason=availability.reason,
        )
    if generated is not None:
        return AbilityGameplayResult(
            ability_id=ability_id,
            display_name=display_name,
            description=description,
            available=True,
            status=AbilityGameplayStatus.UNSUPPORTED,
            error_code="unsupported_generated_mechanic",
            reason="This generated ability has no deterministic gameplay rule yet.",
        )
    player = state.get("player")
    location = player.get("location") if isinstance(player, dict) else None
    if ability_id != "keen_eye" or location != "library":
        return AbilityGameplayResult(
            ability_id=ability_id,
            display_name=display_name,
            description=description,
            available=True,
            status=AbilityGameplayStatus.INELIGIBLE_CONTEXT,
            error_code="no_authored_check_for_context",
            reason="No authored gameplay check applies to this ability in the current context.",
        )
    check_result = resolve_ability_check(state, ability_id, difficulty=2)
    return AbilityGameplayResult(
        ability_id=ability_id,
        display_name=display_name,
        description=description,
        available=True,
        status=AbilityGameplayStatus.RESOLVED,
        check_id="keen_eye_library_inspection",
        check_result=check_result,
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
    "generated_ability_definitions",
    "validate_generated_ability_definition",
    "validate_starter_ability_definitions",
    "resolve_gameplay_ability_check",
]
