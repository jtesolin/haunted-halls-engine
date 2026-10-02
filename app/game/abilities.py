from __future__ import annotations

from dataclasses import dataclass
import re
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from app.game.campaign_state import InvalidCampaignStateError
from app.game.character_progression import (
    MAX_TRACK_POINTS,
    MIN_TRACK_POINTS,
    read_character_progression_state,
)
from app.game.items import (
    ensure_items_state,
    move_room_item_to_inventory,
    resolve_item_ids,
    room_location,
)
from app.game.npcs import SUPERNATURAL_NPC_TAGS
from app.game.world import DEFAULT_WORLD, World, normalize_identifier
from app.schemas.abilities import (
    AbilityAvailabilityResult,
    AbilityAvailabilityStatus,
    AbilityCheckOutcome,
    AbilityCheckResult,
)
from app.schemas.character_progression import ProgressionTrackId
from app.schemas.generated_abilities import (
    AbilityObjectEffect,
    AbilityObjectEffectOperation,
    AbilityObjectState,
    AbilityPresenceEffect,
    AbilitySenseFilter,
    AbilityGameplayResult,
    AbilityGameplayStatus,
    AbilityDetail,
    AbilityDomain,
    AbilityEffect,
    GeneratedAbilityDefinition,
    GeneratedAbilityKind,
)
from app.schemas.chat import (
    NarratorAbilityCheckResult,
    NarratorAbilityGameplayResult,
    NarratorAbilityObjectEffect,
    NarratorAbilityObjectOutcome,
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


@dataclass(frozen=True)
class OwnedAbilityProjection:
    """Player-visible ability details derived from authoritative ownership state."""

    ability_id: str
    display_name: str
    description: str
    available: bool
    availability_reason: str | None


def generated_ability_definitions(state: dict[str, Any]) -> tuple[GeneratedAbilityDefinition, ...]:
    """Read persisted generated definitions without repairing legacy state.

    The missing field is the compatible legacy-campaign representation. Any
    present field must be a complete validated starter set, rather than being
    partially ignored or repaired during a later lookup.
    """
    try:
        player = state.get("player")
        raw_definitions = player.get("generated_abilities") if isinstance(player, dict) else None
        if raw_definitions is None:
            return ()
        if not isinstance(raw_definitions, list):
            raise ValueError("Persisted generated ability definitions must be a list.")
        definitions: list[GeneratedAbilityDefinition] = []
        for raw_definition in raw_definitions:
            definition = GeneratedAbilityDefinition.model_validate(raw_definition)
            validate_generated_ability_definition(definition)
            definitions.append(definition)
        return validate_starter_ability_definitions(definitions, allow_legacy_generic=True)
    except (TypeError, ValueError) as exc:
        raise InvalidCampaignStateError(
            "Persisted generated ability definitions are invalid."
        ) from exc


def validate_generated_ability_definition(definition: GeneratedAbilityDefinition) -> None:
    """Validate generated content against the small engine-owned vocabulary."""
    if definition.ability_id in ABILITY_REGISTRY:
        raise ValueError(f"Generated ability id '{definition.ability_id}' collides with a built-in ability.")
    if not definition.display_name.strip():
        raise ValueError("Generated ability display_name must not be blank.")
    if not definition.description.strip():
        raise ValueError("Generated ability description must not be blank.")
    built_in_names = {
        ability.display_name.strip().casefold()
        for ability in VALIDATED_ABILITY_DEFINITIONS
    }
    if definition.display_name.strip().casefold() in built_in_names:
        raise ValueError(
            f"Generated ability display_name '{definition.display_name}' collides with a built-in ability."
        )
    if definition.kind == GeneratedAbilityKind.SENSORY:
        if definition.mechanics.effect != AbilityEffect.SENSE or definition.mechanics.domain != AbilityDomain.SURROUNDINGS:
            raise ValueError("Sensory abilities must use the surroundings sense mechanic.")
    elif definition.kind == GeneratedAbilityKind.UTILITY:
        if (
            definition.mechanics.effect
            not in {AbilityEffect.MINOR_UTILITY, AbilityEffect.MOVE, AbilityEffect.TOGGLE}
            or definition.mechanics.domain != AbilityDomain.OBJECT
        ):
            raise ValueError("Utility abilities must use a supported object mechanic.")
    if definition.mechanics.detail not in {AbilityDetail.LIMITED, AbilityDetail.PRACTICAL}:
        raise ValueError("Generated ability detail is not supported.")
    if len(definition.mechanics.requires) > 2:
        raise ValueError("Generated ability requirements may contain at most 2 items.")
    if len(definition.mechanics.requires) != len(set(definition.mechanics.requires)):
        raise ValueError("Generated ability requirements must not contain duplicates.")
    if any(requirement not in {"line_of_sight", "nearby"} for requirement in definition.mechanics.requires):
        raise ValueError("Generated ability uses an unsupported requirement.")
    if len(definition.mechanics.bypasses) > 2:
        raise ValueError("Generated ability bypasses may contain at most 2 items.")
    if len(definition.mechanics.bypasses) != len(set(definition.mechanics.bypasses)):
        raise ValueError("Generated ability bypasses must not contain duplicates.")
    if definition.mechanics.bypasses:
        raise ValueError("Generated starter abilities may not bypass core gameplay constraints.")
    is_legacy_generic = definition.mechanics.effect in {
        AbilityEffect.SENSE,
        AbilityEffect.MINOR_UTILITY,
    } and (
        definition.mechanics.sense_filter is None
        and definition.mechanics.object_motion is None
        and definition.mechanics.object_state is None
    )
    if not is_legacy_generic and (
        definition.mechanics.requires != ("nearby",)
    ):
        raise ValueError("Specific generated abilities must require nearby targets.")


def validate_starter_ability_definitions(
    definitions: Sequence[GeneratedAbilityDefinition],
    *,
    allow_legacy_generic: bool = False,
) -> tuple[GeneratedAbilityDefinition, GeneratedAbilityDefinition]:
    """Validate the exactly-two modest starter set before it reaches state."""
    if len(definitions) != 2:
        raise ValueError("Exactly two generated starter abilities are required.")
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    seen_references: set[str] = set()
    kinds: set[GeneratedAbilityKind] = set()
    for definition in definitions:
        validate_generated_ability_definition(definition)
        if not allow_legacy_generic and _is_legacy_generic_mechanic(definition):
            raise ValueError("New generated starters must use a specific supported mechanic.")
        if not allow_legacy_generic:
            _validate_thematic_display_name(definition.display_name)
        if definition.minimum_points != 0:
            raise ValueError("Generated starter abilities must be available at baseline.")
        if definition.ability_id in seen_ids:
            raise ValueError("Generated starter ability ids must be distinct.")
        normalized_name = definition.display_name.strip().casefold()
        if normalized_name in seen_names:
            raise ValueError("Generated starter ability names must be distinct.")
        if not allow_legacy_generic:
            references = {
                normalize_identifier(reference)
                for reference in ability_invocation_references(
                    definition.ability_id, definition.display_name
                )
                if normalize_identifier(reference)
            }
            if references & seen_references:
                raise ValueError("Generated starter invocation references must be distinct.")
            seen_references.update(references)
        seen_ids.add(definition.ability_id)
        seen_names.add(normalized_name)
        kinds.add(definition.kind)
    if kinds != {GeneratedAbilityKind.SENSORY, GeneratedAbilityKind.UTILITY}:
        raise ValueError("Starter abilities require one sensory and one utility definition.")
    return (definitions[0], definitions[1])


def ability_invocation_references(ability_id: str, display_name: str) -> frozenset[str]:
    """Return exact invocation spellings; canonical comparisons use normalize_identifier."""
    return frozenset(
        " ".join(reference.casefold().split())
        for reference in (ability_id, ability_id.replace("_", " "), display_name)
        if reference.strip()
    )


def _is_legacy_generic_mechanic(definition: GeneratedAbilityDefinition) -> bool:
    return (
        definition.mechanics.effect == AbilityEffect.SENSE
        and definition.mechanics.sense_filter is None
    ) or (
        definition.mechanics.effect == AbilityEffect.MINOR_UTILITY
        and definition.mechanics.object_motion is None
        and definition.mechanics.object_state is None
    )


def _validate_thematic_display_name(display_name: str) -> None:
    normalized_name = display_name.casefold()
    words = re.findall(r"[a-z0-9]+(?:'[a-z0-9]+)?", normalized_name)
    taxonomy_terms = {
        "ability",
        "convenience",
        "detection",
        "influence",
        "minor",
        "nearby",
        "object",
        "perception",
        "presence",
        "sensory",
        "supernatural",
        "utility",
    }
    normalized_words = set(re.findall(r"[a-z0-9]+", re.sub(r"'s\b", "", normalized_name)))
    if not 1 <= len(words) <= 3 or (
        normalized_words and normalized_words <= taxonomy_terms
    ):
        raise ValueError("Generated starter display names must be short and thematic.")


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


def project_owned_abilities(state: dict[str, Any]) -> tuple[OwnedAbilityProjection, ...]:
    """Project canonical, owned abilities and their evaluated availability.

    Built-in and persisted generated definitions share the same ownership and
    availability authority. Invalid persisted generated definitions are
    intentionally allowed to raise through the campaign-state integrity
    boundary.
    """
    definitions = (*VALIDATED_ABILITY_DEFINITIONS, *generated_ability_definitions(state))
    projections: list[OwnedAbilityProjection] = []
    for definition in definitions:
        availability = evaluate_ability_availability(state, definition.ability_id)
        if not availability.owned:
            continue
        description = (
            definition.short_description
            if isinstance(definition, AbilityDefinition)
            else _generated_ability_player_facing_summary(definition)
        )
        projections.append(
            OwnedAbilityProjection(
                ability_id=definition.ability_id,
                display_name=definition.display_name,
                description=description,
                available=availability.available,
                availability_reason=availability.reason,
            )
        )
    return tuple(projections)


def _generated_ability_player_facing_summary(
    definition: GeneratedAbilityDefinition,
) -> str:
    """Describe only the bounded meaning of a validated generated mechanic."""
    if (
        definition.kind == GeneratedAbilityKind.SENSORY
        and definition.mechanics.effect == AbilityEffect.SENSE
        and definition.mechanics.domain == AbilityDomain.SURROUNDINGS
    ):
        if definition.mechanics.sense_filter == AbilitySenseFilter.PRESENCE:
            range_text = (
                "this room and directly adjacent rooms"
                if definition.mechanics.range == 1
                else "this room"
            )
            return f"Sense active presence in {range_text}."
        if definition.mechanics.sense_filter == AbilitySenseFilter.SUPERNATURAL_PRESENCE:
            range_text = (
                ", including from directly adjacent spaces"
                if definition.mechanics.range == 1
                else " in this room"
            )
            return f"Sense nearby supernatural presence{range_text}."
        return "Sense faint or unusual changes in nearby surroundings."
    if definition.mechanics.effect == AbilityEffect.MINOR_UTILITY:
        return "Exert a small practical supernatural influence on a nearby ordinary object."
    if definition.mechanics.effect == AbilityEffect.MOVE:
        return "Draw a small portable object from this room into your hand."
    if definition.mechanics.object_state == AbilityObjectState.OPEN:
        return "Open or close a nearby ordinary object that can already be opened."
    if definition.mechanics.object_state == AbilityObjectState.LIT:
        return "Light or extinguish a nearby object that can normally hold a flame."
    raise ValueError("Generated ability mechanics do not have a player-facing summary.")


def project_narrator_ability_gameplay_result(
    result: AbilityGameplayResult,
) -> NarratorAbilityGameplayResult:
    """Keep internal execution diagnostics out of the Narrator's ability outcome."""
    check_result = result.check_result
    check_projection = None
    if check_result is not None and check_result.resolved:
        check_projection = NarratorAbilityCheckResult(
            ability_id=check_result.ability_id,
            outcome=check_result.outcome,
            resolved=check_result.resolved,
            success=check_result.success,
            track_id=check_result.track_id,
            track_points=check_result.track_points,
            difficulty=check_result.difficulty,
            margin=check_result.margin,
        )
    return NarratorAbilityGameplayResult(
        ability_id=result.ability_id,
        display_name=result.display_name,
        description=result.description,
        owned=result.owned,
        available=result.available,
        effect_resolved=result.status == AbilityGameplayStatus.RESOLVED,
        check_id=result.check_id,
        check_result=check_projection,
        presence_effect=result.presence_effect,
        object_effect=(
            NarratorAbilityObjectEffect(
                outcome=_narrator_object_outcome(result.object_effect),
                item_name=result.object_effect.item_name,
            )
            if result.object_effect is not None
            else None
        ),
    )


def _narrator_object_outcome(
    effect: AbilityObjectEffect,
) -> NarratorAbilityObjectOutcome:
    if effect.operation == AbilityObjectEffectOperation.RETRIEVE:
        return NarratorAbilityObjectOutcome.DRAWN_INTO_HAND
    if effect.operation == AbilityObjectEffectOperation.TOGGLE_OPEN:
        return (
            NarratorAbilityObjectOutcome.OPENED
            if effect.new_value
            else NarratorAbilityObjectOutcome.CLOSED
        )
    return (
        NarratorAbilityObjectOutcome.LIT
        if effect.new_value
        else NarratorAbilityObjectOutcome.EXTINGUISHED
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
    state: dict[str, Any],
    ability_id: str,
    target: str | None = None,
    world: World = DEFAULT_WORLD,
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
        description = _generated_ability_player_facing_summary(generated)
    availability = evaluate_ability_availability(state, ability_id)
    if not availability.available:
        return AbilityGameplayResult(
            ability_id=ability_id,
            display_name=display_name,
            description=description,
            owned=availability.owned,
            available=False,
            status=AbilityGameplayStatus.UNAVAILABLE,
            error_code=availability.error_code,
            reason=availability.reason,
        )
    if generated is not None:
        return _resolve_generated_ability_effect(
            state=state,
            definition=generated,
            target=target,
            world=world,
            owned=availability.owned,
            display_name=display_name,
            description=description,
        )
    player = state.get("player")
    location = player.get("location") if isinstance(player, dict) else None
    if ability_id != "keen_eye" or location != "library":
        return AbilityGameplayResult(
            ability_id=ability_id,
            display_name=display_name,
            description=description,
            owned=availability.owned,
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
        owned=availability.owned,
        available=True,
        status=AbilityGameplayStatus.RESOLVED,
        check_id="keen_eye_library_inspection",
        check_result=check_result,
    )


def _resolve_generated_ability_effect(
    *,
    state: dict[str, Any],
    definition: GeneratedAbilityDefinition,
    target: str | None,
    world: World,
    owned: bool,
    display_name: str,
    description: str,
) -> AbilityGameplayResult:
    mechanics = definition.mechanics
    common = {
        "ability_id": definition.ability_id,
        "display_name": display_name,
        "description": description,
        "owned": owned,
        "available": True,
    }
    if _is_legacy_generic_mechanic(definition):
        return AbilityGameplayResult(
            **common,
            status=AbilityGameplayStatus.UNSUPPORTED,
            error_code="unsupported_generated_mechanic",
            reason="This legacy generated ability has no safely mapped operation.",
        )

    player = state.get("player")
    current_room = player.get("location") if isinstance(player, dict) else None
    room = world.get_room(current_room) if isinstance(current_room, str) else None
    if room is None:
        return AbilityGameplayResult(
            **common,
            status=AbilityGameplayStatus.INELIGIBLE_CONTEXT,
            error_code="invalid_current_location",
            reason="The player is not in a valid room.",
        )

    if mechanics.effect == AbilityEffect.SENSE:
        if target is not None:
            return AbilityGameplayResult(
                **common,
                status=AbilityGameplayStatus.INELIGIBLE_CONTEXT,
                error_code="unexpected_target",
                reason="This ability does not take an object target.",
            )
        sense_filter = mechanics.sense_filter
        if sense_filter is None:
            return AbilityGameplayResult(
                **common,
                status=AbilityGameplayStatus.UNSUPPORTED,
                error_code="unsupported_generated_mechanic",
                reason="This legacy generated ability has no safely mapped operation.",
            )
        adjacent_room_ids = (
            {
                adjacent
                for adjacent in room.exits.values()
                if world.get_room(adjacent) is not None
            }
            if mechanics.range == 1
            else set()
        )
        npc_state = state.get("npcs")
        current_count = 0
        adjacent_count = 0
        if isinstance(npc_state, dict):
            for npc in npc_state.values():
                if not isinstance(npc, dict) or npc.get("status") != "active":
                    continue
                if sense_filter == AbilitySenseFilter.SUPERNATURAL_PRESENCE:
                    tags = npc.get("tags")
                    if not isinstance(tags, list) or not any(
                        isinstance(tag, str) and tag.casefold() in SUPERNATURAL_NPC_TAGS
                        for tag in tags
                    ):
                        continue
                location = npc.get("location")
                if location == room.id:
                    current_count += 1
                elif location in adjacent_room_ids:
                    adjacent_count += 1
        presence_effect = AbilityPresenceEffect(
            sense_filter=sense_filter,
            found=current_count + adjacent_count > 0,
            current_room_count=current_count,
            adjacent_room_count=adjacent_count,
        )
        return AbilityGameplayResult(
            **common,
            status=AbilityGameplayStatus.RESOLVED,
            presence_effect=presence_effect,
        )

    if not isinstance(target, str) or not target.strip():
        return AbilityGameplayResult(
            **common,
            status=AbilityGameplayStatus.INELIGIBLE_CONTEXT,
            error_code="target_missing",
            reason="An object target is required.",
        )

    items = ensure_items_state(state)
    room_items = {
        item_id: item
        for item_id, item in items.items()
        if item.get("location") == room_location(room.id)
    }
    target_matches = resolve_item_ids(room_items, target)
    if len(target_matches) > 1:
        return AbilityGameplayResult(
            **common,
            status=AbilityGameplayStatus.INELIGIBLE_CONTEXT,
            error_code="target_ambiguous",
            reason="The requested object matches more than one nearby item.",
        )
    if not target_matches:
        global_matches = resolve_item_ids(items, target)
        error_code = "target_not_nearby" if global_matches else "target_missing"
        reason = (
            "The requested object is not in this room."
            if global_matches
            else "No item matches the requested target."
        )
        return AbilityGameplayResult(
            **common,
            status=AbilityGameplayStatus.INELIGIBLE_CONTEXT,
            error_code=error_code,
            reason=reason,
        )

    item_id = target_matches[0]
    item = items[item_id]
    raw_item_name = item.get("name")
    item_name: str = raw_item_name if isinstance(raw_item_name, str) else str(item_id)
    if mechanics.effect == AbilityEffect.MOVE:
        if item.get("portable") is not True:
            return AbilityGameplayResult(
                **common,
                status=AbilityGameplayStatus.INELIGIBLE_CONTEXT,
                error_code="target_not_portable",
                reason="The requested object cannot be carried.",
            )
        move_room_item_to_inventory(state, items, item_id, room.id)
        object_effect = AbilityObjectEffect(
            operation=AbilityObjectEffectOperation.RETRIEVE,
            item_id=item_id,
            item_name=item_name,
        )
    else:
        properties = item.get("properties")
        object_state = mechanics.object_state
        property_name = (
            "is_open"
            if object_state == AbilityObjectState.OPEN
            else "lit"
            if object_state == AbilityObjectState.LIT
            else None
        )
        capability_name = (
            "openable" if object_state == AbilityObjectState.OPEN else "lightable"
        )
        if (
            property_name is None
            or not isinstance(properties, dict)
            or properties.get(capability_name) is not True
            or not isinstance(properties.get(property_name), bool)
        ):
            error_code = (
                "target_not_openable"
                if object_state == AbilityObjectState.OPEN
                else "target_not_lightable"
            )
            reason = (
                "The requested object cannot be opened or closed."
                if object_state == AbilityObjectState.OPEN
                else "The requested object cannot be lit or extinguished."
            )
            return AbilityGameplayResult(
                **common,
                status=AbilityGameplayStatus.INELIGIBLE_CONTEXT,
                error_code=error_code,
                reason=reason,
            )
        if (
            object_state == AbilityObjectState.OPEN
            and "locked" in properties
            and properties["locked"] is not False
        ):
            return AbilityGameplayResult(
                **common,
                status=AbilityGameplayStatus.INELIGIBLE_CONTEXT,
                error_code="target_locked",
                reason="The requested object is locked.",
            )
        previous_value = properties[property_name]
        new_value = not previous_value
        properties[property_name] = new_value
        object_effect = AbilityObjectEffect(
            operation=(
                AbilityObjectEffectOperation.TOGGLE_OPEN
                if property_name == "is_open"
                else AbilityObjectEffectOperation.TOGGLE_LIT
            ),
            item_id=item_id,
            item_name=item_name,
            previous_value=previous_value,
            new_value=new_value,
        )
    return AbilityGameplayResult(
        **common,
        status=AbilityGameplayStatus.RESOLVED,
        object_effect=object_effect,
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
    "OwnedAbilityProjection",
    "project_owned_abilities",
    "resolve_ability_check",
    "generated_ability_definitions",
    "validate_generated_ability_definition",
    "validate_starter_ability_definitions",
    "resolve_gameplay_ability_check",
]
