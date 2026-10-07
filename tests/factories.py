import json
import re

from app.game.world import normalize_identifier
from app.schemas.character_progression import ProgressionTrackId
from app.schemas.chat import ActionParserOutput, ActionParserParameters, ActionType
from app.schemas.generated_abilities import (
    AbilityChannel,
    AbilityDetail,
    AbilityDomain,
    AbilityEffect,
    AbilityObjectMotion,
    AbilitySenseFilter,
    GeneratedAbilityDefinition,
    GeneratedAbilityKind,
    GeneratedAbilityMechanics,
    StarterAbilityGeneration,
    TraversalMethod,
)
from app.agents.starter_abilities import StarterAbilityGenerator
from app.schemas.starter_ability_provider import (
    StarterAbilityProviderGeneration,
    StarterAbilityTraversalProviderOutput,
)


def traversal_ability_generation(method: TraversalMethod) -> StarterAbilityGeneration:
    """Exercise deterministic provider conversion without a provider call."""
    other = (
        TraversalMethod.WATER_WALKING
        if method != TraversalMethod.WATER_WALKING else TraversalMethod.LEVITATION
    )
    return StarterAbilityGenerator()._to_domain_generation(
        StarterAbilityProviderGeneration(
            first_ability=StarterAbilityTraversalProviderOutput(
                ability_id="silver_step", display_name="Silver Step",
                description="Purely cosmetic silver shimmer.", track=ProgressionTrackId.OCCULT,
                traversal_method=method,
            ),
            second_ability=StarterAbilityTraversalProviderOutput(
                ability_id="mist_stride", display_name="Mist Stride",
                description="Purely cosmetic mist.", track=ProgressionTrackId.OCCULT,
                traversal_method=other,
            ),
        )
    )


def starter_ability_generation() -> StarterAbilityGeneration:
    """Build deterministic starter abilities for tests without a provider."""
    return StarterAbilityGeneration(
        abilities=[
            GeneratedAbilityDefinition(
                ability_id="echo_sense",
                display_name="Grave Echo",
                description="Feel supernatural presence through a faint chill in the air.",
                kind=GeneratedAbilityKind.SENSORY,
                mechanics=GeneratedAbilityMechanics(
                    effect=AbilityEffect.SENSE,
                    domain=AbilityDomain.SURROUNDINGS,
                    channel=AbilityChannel.SUPERNATURAL,
                    detail=AbilityDetail.LIMITED,
                    range=1,
                    requires=("nearby",),
                    sense_filter=AbilitySenseFilter.SUPERNATURAL_PRESENCE,
                ),
                track=ProgressionTrackId.INVESTIGATION,
                minimum_points=0,
            ),
            GeneratedAbilityDefinition(
                ability_id="whispering_touch",
                display_name="Whispering Grasp",
                description="Draw a small object near with a quiet, unseen pull.",
                kind=GeneratedAbilityKind.UTILITY,
                mechanics=GeneratedAbilityMechanics(
                    effect=AbilityEffect.MOVE,
                    domain=AbilityDomain.OBJECT,
                    channel=AbilityChannel.SUPERNATURAL,
                    detail=AbilityDetail.PRACTICAL,
                    range=0,
                    requires=("nearby",),
                    object_motion=AbilityObjectMotion.TOWARD_PLAYER,
                ),
                track=ProgressionTrackId.OCCULT,
                minimum_points=0,
            ),
        ]
    )


def fake_action_parser_output(messages: list[dict[str, object]]) -> ActionParserOutput:
    """Return a structured provider double for orchestration tests."""
    player_text = ""
    context: dict[str, object] = {}
    for message in messages:
        content = message.get("content")
        if not isinstance(content, str):
            continue
        if content.startswith("Parser context:\n"):
            parsed_context = json.loads(content.removeprefix("Parser context:\n"))
            if isinstance(parsed_context, dict):
                context = parsed_context
        if content.startswith("Player text:\n"):
            player_text = content.removeprefix("Player text:\n")

    lowered = player_text.casefold()
    abilities = context.get("abilities")
    ability_matches: list[tuple[int, str, bool]] = []
    if isinstance(abilities, list):
        for ability in abilities:
            if not isinstance(ability, dict) or ability.get("available") is not True:
                continue
            ability_id = ability.get("ability_id")
            name = ability.get("name")
            if not isinstance(ability_id, str) or not isinstance(name, str):
                continue
            references = {normalize_identifier(ability_id), normalize_identifier(name)}
            for reference in references:
                if not reference:
                    continue
                phrase = r"[\W_]+".join(re.escape(part) for part in reference.split())
                if re.search(r"(?<![a-z0-9])" + phrase + r"(?![a-z0-9])", lowered):
                    ability_matches.append(
                        (len(reference), ability_id, ability.get("requires_target") is True)
                    )
    if ability_matches:
        _, ability_id, requires_target = max(ability_matches)
        target = None
        target_match = re.search(
            r"\b(?:on|at|toward|towards)\s+(?:the|a|an)\s+(.+?)(?:[.!?]|$)",
            player_text,
            re.IGNORECASE,
        )
        if target_match is None:
            target_match = re.search(
                r"\b(?:on|at|toward|towards)\s+(.+?)(?:[.!?]|$)",
                player_text,
                re.IGNORECASE,
            )
        if target_match is not None:
            target = target_match.group(1).strip()
        elif requires_target:
            target = None
        return ActionParserOutput(
            action=ActionType.ABILITY_CHECK,
            target=target,
            parameters=ActionParserParameters(ability_id=ability_id),
            confidence=1,
            parse_status="ok",
        )

    if any(word in lowered for word in ("north", "south", "east", "west")) or re.search(
        r"\b(?:go|move|walk|run|enter)\b", lowered
    ):
        direction = next(
            (word for word in ("north", "south", "east", "west") if word in lowered),
            None,
        )
        target = direction or ("library" if "library" in lowered else "entry_hall")
        action = ActionType.MOVE
    elif any(word in lowered for word in ("take ", "grab ", "pick up ", "collect ")):
        action = ActionType.TAKE
        target = "old_book" if "book" in lowered else "brass_key" if "key" in lowered else None
    elif any(word in lowered for word in ("drop ", "discard ")):
        action = ActionType.DROP
        target = "brass_key" if "key" in lowered else None
    elif any(word in lowered for word in ("talk ", "speak ", "ask ")):
        action = ActionType.TALK
        target = "library_ghost" if "ghost" in lowered else "old_caretaker"
    elif "wait" in lowered or "rest" in lowered:
        action = ActionType.WAIT
        target = None
    elif "light " in lowered:
        action = ActionType.USE
        target = "candle"
    elif "open " in lowered or "close " in lowered:
        action = ActionType.INTERACT
        target = "cellar_door" if "door" in lowered else "old_book"
    else:
        action = ActionType.OBSERVE
        target = None
    return ActionParserOutput(
        action=action,
        target=target,
        parameters=ActionParserParameters(),
        confidence=1,
        parse_status="ok",
    )
