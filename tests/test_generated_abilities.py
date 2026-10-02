from __future__ import annotations

import asyncio
import copy
import json

import pytest
from openai.lib._pydantic import to_strict_json_schema

from app.agents import starter_abilities as starter_abilities_module
from app.agents.action_parser import ActionParserAgent
from app.agents.starter_abilities import StarterAbilityGenerator
from app.agents.narrator import NarratorAgent, NarratorAgentInput
from app.ai.model_client import ModelCallResult
from app.game.abilities import (
    evaluate_ability_availability,
    generated_ability_definitions,
    project_narrator_ability_gameplay_result,
    project_owned_abilities,
    resolve_gameplay_ability_check,
    validate_starter_ability_definitions,
)
from app.game.campaign_state import (
    InvalidCampaignStateError,
    build_fresh_campaign_state,
    load_authoritative_campaign_state,
)
from app.game.character_progression import ensure_character_progression_state, unlock_ability
from app.game.items import room_location
from app.game.narrator_scene import build_narrator_scene_context
from app.guardrails.model_policy import ModelPolicy
from app.guardrails.token_budget import TokenBudget
from app.schemas.abilities import AbilityCheckOutcome, AbilityCheckResult
from app.schemas.character_progression import ProgressionTrackId
from app.schemas.chat import ActionParserOutput, ActionParserParameters, ActionType, ParsedAction
from app.schemas.generated_abilities import (
    AbilityChannel,
    AbilityDetail,
    AbilityDomain,
    AbilityEffect,
    AbilityGameplayResult,
    AbilityGameplayStatus,
    AbilityObjectMotion,
    AbilityObjectState,
    AbilitySenseFilter,
    GeneratedAbilityMechanics,
    StarterAbilityGeneration,
)
from app.services import tool_executor as tool_executor_module
from app.services.tool_executor import ToolExecutor
from app.tools.registry import ToolRegistry

_STARTER_GENERATE = StarterAbilityGenerator.generate


def _state_with_starters() -> dict:
    state = build_fresh_campaign_state()
    generated = StarterAbilityGenerator()._stub_generation()
    state["player"]["generated_abilities"] = [
        ability.model_dump(mode="json") for ability in generated.abilities
    ]
    ensure_character_progression_state(state)
    for ability in generated.abilities:
        assert unlock_ability(state, ability.ability_id).success
    return state


def _mechanics(
    *,
    effect: AbilityEffect,
    domain: AbilityDomain,
    detail: AbilityDetail,
    range: int,
    sense_filter: AbilitySenseFilter | None = None,
    object_motion: AbilityObjectMotion | None = None,
    object_state: AbilityObjectState | None = None,
) -> GeneratedAbilityMechanics:
    return GeneratedAbilityMechanics(
        effect=effect,
        domain=domain,
        channel=AbilityChannel.SUPERNATURAL,
        detail=detail,
        range=range,
        requires=("nearby",),
        sense_filter=sense_filter,
        object_motion=object_motion,
        object_state=object_state,
    )


def _set_mechanics(
    state: dict,
    ability_index: int,
    mechanics: GeneratedAbilityMechanics,
) -> None:
    state["player"]["generated_abilities"][ability_index]["mechanics"] = (
        mechanics.model_dump(mode="json")
    )


def _execute_generated(
    state: dict,
    ability_id: str,
    target: str | None = None,
):
    return ToolExecutor().execute(
        parsed_action=ParsedAction(
            raw_text=f"use {ability_id.replace('_', ' ')}",
            action=ActionType.ABILITY_CHECK,
            target=target,
            parameters={"ability_id": ability_id},
            confidence=1,
            parse_status="ok",
        ),
        campaign_state=json.dumps(state),
    )


def test_starter_ability_provider_schema_uses_bounded_homogeneous_array() -> None:
    abilities_schema = StarterAbilityGeneration.model_json_schema()["properties"]["abilities"]

    assert abilities_schema["type"] == "array"
    assert "items" in abilities_schema
    assert abilities_schema["minItems"] == 2
    assert abilities_schema["maxItems"] == 2
    assert "prefixItems" not in abilities_schema


def test_new_starters_validate_all_supported_specific_utility_variants() -> None:
    sensory = StarterAbilityGenerator()._stub_generation().abilities[0]
    utility = StarterAbilityGenerator()._stub_generation().abilities[1]
    variants = (
        _mechanics(
            effect=AbilityEffect.MOVE,
            domain=AbilityDomain.OBJECT,
            detail=AbilityDetail.PRACTICAL,
            range=0,
            object_motion=AbilityObjectMotion.TOWARD_PLAYER,
        ),
        _mechanics(
            effect=AbilityEffect.TOGGLE,
            domain=AbilityDomain.OBJECT,
            detail=AbilityDetail.PRACTICAL,
            range=0,
            object_state=AbilityObjectState.OPEN,
        ),
        _mechanics(
            effect=AbilityEffect.TOGGLE,
            domain=AbilityDomain.OBJECT,
            detail=AbilityDetail.PRACTICAL,
            range=0,
            object_state=AbilityObjectState.LIT,
        ),
    )

    for mechanics in variants:
        candidate = utility.model_copy(update={"mechanics": mechanics})
        validated = validate_starter_ability_definitions((sensory, candidate))
        assert validated[1].mechanics == mechanics


def test_generated_ability_schema_rejects_contradictory_primitives() -> None:
    with pytest.raises(ValueError, match="contradictory"):
        _mechanics(
            effect=AbilityEffect.MOVE,
            domain=AbilityDomain.OBJECT,
            detail=AbilityDetail.PRACTICAL,
            range=0,
            sense_filter=AbilitySenseFilter.PRESENCE,
            object_motion=AbilityObjectMotion.TOWARD_PLAYER,
        )

    with pytest.raises(ValueError, match="contradictory"):
        _mechanics(
            effect=AbilityEffect.SENSE,
            domain=AbilityDomain.SURROUNDINGS,
            detail=AbilityDetail.LIMITED,
            range=0,
            object_state=AbilityObjectState.OPEN,
        )

    with pytest.raises(ValueError, match="contradictory"):
        _mechanics(
            effect=AbilityEffect.TOGGLE,
            domain=AbilityDomain.OBJECT,
            detail=AbilityDetail.PRACTICAL,
            range=0,
        )

    with pytest.raises(ValueError, match="contradictory"):
        _mechanics(
            effect=AbilityEffect.MOVE,
            domain=AbilityDomain.OBJECT,
            detail=AbilityDetail.PRACTICAL,
            range=1,
            object_motion=AbilityObjectMotion.TOWARD_PLAYER,
        )

    for effect, domain, detail, sense_filter, object_motion, object_state in (
        (
            AbilityEffect.SENSE,
            AbilityDomain.SURROUNDINGS,
            AbilityDetail.PRACTICAL,
            AbilitySenseFilter.PRESENCE,
            None,
            None,
        ),
        (
            AbilityEffect.MOVE,
            AbilityDomain.OBJECT,
            AbilityDetail.LIMITED,
            None,
            AbilityObjectMotion.TOWARD_PLAYER,
            None,
        ),
        (
            AbilityEffect.TOGGLE,
            AbilityDomain.OBJECT,
            AbilityDetail.LIMITED,
            None,
            None,
            AbilityObjectState.OPEN,
        ),
    ):
        with pytest.raises(ValueError, match="contradictory"):
            _mechanics(
                effect=effect,
                domain=domain,
                detail=detail,
                range=0,
                sense_filter=sense_filter,
                object_motion=object_motion,
                object_state=object_state,
            )


def test_generated_ability_structured_output_schema_has_closed_bounded_fields() -> None:
    schema = to_strict_json_schema(StarterAbilityGeneration)
    definitions = schema["$defs"]
    mechanics = definitions["GeneratedAbilityMechanics"]

    assert mechanics["additionalProperties"] is False
    assert set(mechanics["properties"]) == set(mechanics["required"])
    assert "anyOf" in mechanics["properties"]["sense_filter"]
    assert set(mechanics["properties"]) >= {
        "effect",
        "domain",
        "sense_filter",
        "object_motion",
        "object_state",
        "range",
        "requires",
        "bypasses",
    }
    effect_values = set(definitions["AbilityEffect"]["enum"])
    assert effect_values == {"sense", "minor_utility", "move", "toggle"}
    assert schema["properties"]["abilities"]["type"] == "array"
    assert "prefixItems" not in schema["properties"]["abilities"]


def test_provider_disabled_starters_are_valid_distinct_and_available() -> None:
    generation = asyncio.run(
        StarterAbilityGenerator().generate(provider_model_enabled=False)
    )
    abilities = validate_starter_ability_definitions(generation.abilities)
    state = _state_with_starters()

    assert len(abilities) == 2
    assert {ability.kind.value for ability in abilities} == {"sensory", "utility"}
    assert len({ability.ability_id for ability in abilities}) == 2
    assert {ability.display_name for ability in abilities} == {
        "Grave Echo",
        "Whispering Grasp",
    }
    assert {ability.mechanics.effect for ability in abilities} == {
        AbilityEffect.SENSE,
        AbilityEffect.MOVE,
    }
    assert all(evaluate_ability_availability(state, ability.ability_id).available for ability in abilities)
    assert "keen_eye" not in state["player"]["progression"]["unlocked_abilities"]


def test_starter_generation_uses_dedicated_bounded_reasoning_policy(monkeypatch) -> None:
    monkeypatch.setattr(StarterAbilityGenerator, "generate", _STARTER_GENERATE)
    generation = StarterAbilityGenerator()._stub_generation()
    captured: dict[str, object] = {}

    async def fake_generate_structured(**kwargs):  # noqa: ANN003, ANN202
        captured.update(kwargs)
        return ModelCallResult(output=generation)

    monkeypatch.setattr(
        starter_abilities_module.model_client,
        "generate_structured",
        fake_generate_structured,
    )

    result = asyncio.run(
        StarterAbilityGenerator().generate(
            provider_model_enabled=True,
            return_usage=True,
        )
    )

    assert result.output == generation
    assert captured["model"] == ModelPolicy.narrator_model()
    assert captured["reasoning_effort"] == "minimal"
    assert captured["reasoning_effort"] == ModelPolicy.starter_ability_reasoning_effort()
    assert captured["reasoning_effort"] != ModelPolicy.narrator_reasoning_effort()
    assert captured["max_output_tokens"] == 800
    assert captured["max_output_tokens"] == TokenBudget.starter_ability_max_output_tokens()


def test_starter_generation_prompt_bounds_names_and_descriptions() -> None:
    prompt = "\n".join(
        str(message.get("content", ""))
        for message in StarterAbilityGenerator().build_provider_request()
    ).lower()

    for requirement in (
        "thematic, evocative name",
        "one to three short words",
        "sense_filter presence or supernatural_presence",
        "move/object/toward_player",
        "toggle/object/open",
        "toggle/object/lit",
        "only the validated mechanics define what it does",
        "read minds",
        "see remotely",
        "invisibility",
        "bypass darkness or invisibility",
        "unlock objects",
        "move entities",
        "damage or attack",
        "change quest or world state",
    ):
        assert requirement in prompt


def test_starter_generator_preserves_missing_output_when_usage_is_requested(
    monkeypatch,
) -> None:
    monkeypatch.setattr(StarterAbilityGenerator, "generate", _STARTER_GENERATE)
    missing_output = ModelCallResult(
        output=None,
        status="incomplete",
        incomplete_details_reason="max_output_tokens",
    )

    async def fake_generate_structured(**kwargs):  # noqa: ANN003, ANN202
        return missing_output

    monkeypatch.setattr(
        starter_abilities_module.model_client,
        "generate_structured",
        fake_generate_structured,
    )

    result = asyncio.run(
        StarterAbilityGenerator().generate(
            provider_model_enabled=True,
            return_usage=True,
        )
    )

    assert result is missing_output


def test_starter_generator_fails_explicitly_without_usage_when_output_is_missing(
    monkeypatch,
) -> None:
    monkeypatch.setattr(StarterAbilityGenerator, "generate", _STARTER_GENERATE)

    async def fake_generate_structured(**kwargs):  # noqa: ANN003, ANN202
        return None

    monkeypatch.setattr(
        starter_abilities_module.model_client,
        "generate_structured",
        fake_generate_structured,
    )

    with pytest.raises(ValueError, match="did not return valid structured output"):
        asyncio.run(
            StarterAbilityGenerator().generate(provider_model_enabled=True)
        )


def test_invalid_generated_content_and_built_in_collision_are_rejected() -> None:
    generation = StarterAbilityGenerator()._stub_generation()
    collided = generation.abilities[0].model_copy(update={"ability_id": "keen_eye"})
    with pytest.raises(ValueError, match="collides"):
        validate_starter_ability_definitions((collided, generation.abilities[1]))

    display_name_collided = generation.abilities[0].model_copy(update={"display_name": "kEeN eYe"})
    with pytest.raises(ValueError, match="collides"):
        validate_starter_ability_definitions((display_name_collided, generation.abilities[1]))

    unsupported = generation.abilities[0].model_copy(
        update={"mechanics": generation.abilities[0].mechanics.model_copy(update={"bypasses": ("darkness",)})}
    )
    with pytest.raises(ValueError, match="bypass"):
        validate_starter_ability_definitions((unsupported, generation.abilities[1]))

    unavailable = generation.abilities[0].model_copy(update={"minimum_points": 1})
    with pytest.raises(ValueError, match="baseline"):
        validate_starter_ability_definitions((unavailable, generation.abilities[1]))


def test_generated_mechanic_collections_have_bounded_distinct_values() -> None:
    generation = StarterAbilityGenerator()._stub_generation()

    too_many_requirements = generation.abilities[0].model_copy(
        update={
            "mechanics": generation.abilities[0].mechanics.model_copy(
                update={"requires": ("nearby", "line_of_sight", "nearby")}
            )
        }
    )
    with pytest.raises(ValueError, match="at most 2 items"):
        validate_starter_ability_definitions((too_many_requirements, generation.abilities[1]))

    duplicate_requirements = generation.abilities[0].model_copy(
        update={
            "mechanics": generation.abilities[0].mechanics.model_copy(
                update={"requires": ("nearby", "nearby")}
            )
        }
    )
    with pytest.raises(ValueError, match="duplicates"):
        validate_starter_ability_definitions((duplicate_requirements, generation.abilities[1]))

    bypass = generation.abilities[0].model_copy(
        update={
            "mechanics": generation.abilities[0].mechanics.model_copy(
                update={"bypasses": ("darkness",)}
            )
        }
    )
    with pytest.raises(ValueError, match="bypass"):
        validate_starter_ability_definitions((bypass, generation.abilities[1]))


def test_blank_generated_display_text_is_rejected() -> None:
    generation = StarterAbilityGenerator()._stub_generation()

    blank_name = generation.abilities[0].model_copy(update={"display_name": "   "})
    with pytest.raises(ValueError, match="display_name"):
        validate_starter_ability_definitions((blank_name, generation.abilities[1]))

    blank_description = generation.abilities[0].model_copy(update={"description": "\t\n"})
    with pytest.raises(ValueError, match="description"):
        validate_starter_ability_definitions((blank_description, generation.abilities[1]))


def test_new_starter_names_reject_raw_taxonomy_labels_without_a_fixed_catalog() -> None:
    generation = StarterAbilityGenerator()._stub_generation()
    taxonomy = generation.abilities[0].model_copy(
        update={"display_name": "Supernatural Presence Detection"}
    )
    too_long = generation.abilities[1].model_copy(
        update={"display_name": "Whispering Spectral Object Grasp"}
    )

    with pytest.raises(ValueError, match="short and thematic"):
        validate_starter_ability_definitions((taxonomy, generation.abilities[1]))
    with pytest.raises(ValueError, match="short and thematic"):
        validate_starter_ability_definitions((generation.abilities[0], too_long))


@pytest.mark.parametrize(
    "display_name",
    [
        "Supernatural-Presence Detection",
        "Object_Influence",
        "Nearby.Object.Convenience",
        "Sensory/Ability",
        "Object's Influence",
    ],
)
def test_punctuation_does_not_hide_taxonomy_names(display_name: str) -> None:
    generation = StarterAbilityGenerator()._stub_generation()
    candidate = generation.abilities[0].model_copy(update={"display_name": display_name})
    with pytest.raises(ValueError, match="short and thematic"):
        validate_starter_ability_definitions((candidate, generation.abilities[1]))


@pytest.mark.parametrize(
    "display_name", ["Grave Echo", "Witch's Ear", "Whispering Grasp", "Pale Ember", "Veil-Borne Whisper"]
)
def test_thematic_name_validation_retains_natural_possessives(display_name: str) -> None:
    generation = StarterAbilityGenerator()._stub_generation()
    candidate = generation.abilities[0].model_copy(update={"display_name": display_name})
    other = generation.abilities[1].model_copy(update={"display_name": "Phantom Hand"})
    assert validate_starter_ability_definitions((candidate, other))[0] == candidate


@pytest.mark.parametrize("display_name", ["Echo Sense", "  ECHO   SENSE  ", "echo_sense"])
def test_new_starters_reject_cross_name_id_invocation_collisions(display_name: str) -> None:
    generation = StarterAbilityGenerator()._stub_generation()
    candidate = generation.abilities[1].model_copy(update={"display_name": display_name})
    with pytest.raises(ValueError, match="invocation references"):
        validate_starter_ability_definitions((generation.abilities[0], candidate))

    assert validate_starter_ability_definitions(
        (generation.abilities[0], candidate), allow_legacy_generic=True
    )[1] == candidate


def test_explicit_keen_eye_request_is_a_typed_action_only_when_available() -> None:
    state = build_fresh_campaign_state()
    ensure_character_progression_state(state)
    state["player"]["progression"]["tracks"]["investigation"] = 2
    unlock_ability(state, "keen_eye")
    parsed = asyncio.run(
        ActionParserAgent().parse(
            message="look more closely using keen eye",
            campaign_state=json.dumps(state),
            recent_turns=[],
            deterministic_only=True,
        )
    )

    assert parsed.action == ActionType.ABILITY_CHECK
    assert parsed.parameters == {"ability_id": "keen_eye"}


def test_keen_eye_library_check_is_deterministic_and_non_mutating() -> None:
    state = build_fresh_campaign_state()
    state["player"]["location"] = "library"
    ensure_character_progression_state(state)
    state["player"]["progression"]["tracks"]["investigation"] = 2
    unlock_ability(state, "keen_eye")
    original = copy.deepcopy(state)

    updated, tool_result = ToolExecutor().execute(
        parsed_action=ParsedAction(
            raw_text="use keen eye",
            action=ActionType.ABILITY_CHECK,
            parameters={"ability_id": "keen_eye"},
            confidence=1,
            parse_status="ok",
        ),
        campaign_state=json.dumps(state),
    )

    assert updated == original
    assert tool_result.ability_result is not None
    assert tool_result.ability_result.status == AbilityGameplayStatus.RESOLVED
    assert tool_result.ability_result.check_id == "keen_eye_library_inspection"
    assert tool_result.ability_result.check_result is not None
    assert tool_result.ability_result.check_result.difficulty == 2
    assert tool_result.ability_result.check_result.success is True
    narrator_result = project_narrator_ability_gameplay_result(tool_result.ability_result)
    assert narrator_result.effect_resolved is True
    assert narrator_result.check_result is not None
    assert narrator_result.check_result.difficulty == 2
    assert narrator_result.check_result.success is True
    assert narrator_result.check_id == "keen_eye_library_inspection"


def test_resolved_failed_ability_check_keeps_outer_success_false(monkeypatch) -> None:
    state = build_fresh_campaign_state()

    def failed_keen_eye_check(
        state,
        ability_id,
        *,
        target=None,
        world=None,
    ):  # noqa: ANN001, ARG001, ANN202
        return AbilityGameplayResult(
            ability_id="keen_eye",
            display_name="Keen Eye",
            owned=True,
            available=True,
            status=AbilityGameplayStatus.RESOLVED,
            check_id="keen_eye_library_inspection",
            check_result=AbilityCheckResult(
                ability_id="keen_eye",
                outcome=AbilityCheckOutcome.FAILURE,
                resolved=True,
                success=False,
                track_id=ProgressionTrackId.INVESTIGATION,
                track_points=1,
                difficulty=2,
                margin=-1,
            ),
        )

    monkeypatch.setattr(
        tool_executor_module,
        "resolve_gameplay_ability_check",
        failed_keen_eye_check,
    )

    _updated, tool_result = ToolExecutor().execute(
        parsed_action=ParsedAction(
            raw_text="use keen eye",
            action=ActionType.ABILITY_CHECK,
            parameters={"ability_id": "keen_eye"},
            confidence=1,
            parse_status="ok",
        ),
        campaign_state=json.dumps(state),
    )

    assert tool_result.success is False
    assert tool_result.applied_tools == ["resolve_ability_check"]
    assert tool_result.summary == "Keen Eye check failed."
    assert tool_result.ability_result is not None
    assert tool_result.ability_result.status == AbilityGameplayStatus.RESOLVED
    assert tool_result.ability_result.check_result is not None
    assert tool_result.ability_result.check_result.success is False
    narrator_result = project_narrator_ability_gameplay_result(tool_result.ability_result)
    assert narrator_result.effect_resolved is True
    assert narrator_result.check_result is not None
    assert narrator_result.check_result.resolved is True
    assert narrator_result.check_result.success is False


def test_generated_ability_matching_requires_bounded_explicit_reference() -> None:
    state = _state_with_starters()
    ability_use = asyncio.run(
        ActionParserAgent().parse(
            message="use Whispering Grasp on the brass key",
            campaign_state=json.dumps(state),
            recent_turns=[],
            deterministic_only=True,
        )
    )
    assert ability_use.action == ActionType.ABILITY_CHECK
    assert ability_use.parameters == {"ability_id": "whispering_touch"}
    assert ability_use.target == "brass key"
    missing_target = asyncio.run(
        ActionParserAgent().parse(
            message="use Whispering Grasp",
            campaign_state=json.dumps(state),
            recent_turns=[],
            deterministic_only=True,
        )
    )
    assert missing_target.action == ActionType.ABILITY_CHECK
    assert missing_target.parameters == {"ability_id": "whispering_touch"}
    assert missing_target.target is None
    parser_context = ActionParserAgent()._build_parser_context(json.dumps(state))
    grasp_context = next(
        ability
        for ability in parser_context.abilities
        if ability["ability_id"] == "whispering_touch"
    )
    assert grasp_context["name"] == "Whispering Grasp"
    assert grasp_context["requires_target"] is True

    generated = StarterAbilityGenerator()._stub_generation()
    light_ability = generated.abilities[1].model_copy(
        update={"ability_id": "ghost_light", "display_name": "Light"}
    )
    state["player"]["generated_abilities"] = [
        generated.abilities[0].model_dump(mode="json"),
        light_ability.model_dump(mode="json"),
    ]
    assert unlock_ability(state, "ghost_light").success

    item_use = asyncio.run(
        ActionParserAgent().parse(
            message="light the candle with match",
            campaign_state=json.dumps(state),
            recent_turns=[],
            deterministic_only=True,
        )
    )
    assert item_use.action == ActionType.USE
    assert item_use.target == "candle"
    assert item_use.parameters == {"interaction_mode": "light", "with_item": "match"}

    item_use_with_target = asyncio.run(
        ActionParserAgent().parse(
            message="use light on candle",
            campaign_state=json.dumps(state),
            recent_turns=[],
            deterministic_only=True,
        )
    )
    assert item_use_with_target.action == ActionType.USE
    assert item_use_with_target.target == "candle"
    assert item_use_with_target.parameters == {"with_item": "light"}

    explicitly_invoked_ability = asyncio.run(
        ActionParserAgent().parse(
            message="use my ability Light on candle",
            campaign_state=json.dumps(state),
            recent_turns=[],
            deterministic_only=True,
        )
    )
    assert explicitly_invoked_ability.action == ActionType.ABILITY_CHECK
    assert explicitly_invoked_ability.parameters == {"ability_id": "ghost_light"}
    assert explicitly_invoked_ability.target == "candle"

    ability_use = asyncio.run(
        ActionParserAgent().parse(
            message="use light",
            campaign_state=json.dumps(state),
            recent_turns=[],
            deterministic_only=True,
        )
    )
    assert ability_use.action == ActionType.ABILITY_CHECK
    assert ability_use.parameters == {"ability_id": "ghost_light"}


@pytest.mark.parametrize(
    ("message", "expected_id", "expected_target"),
    [
        ("use Grave Echo", "whispering_touch", None),
        ("use Grave Echo on the brass key", "whispering_touch", "brass key"),
        ("use Grave", "echo_sense", None),
        ("use whispering_touch on the brass key", "whispering_touch", "brass key"),
        ("use whispering touch on the brass key", "whispering_touch", "brass key"),
    ],
)
def test_complete_invocation_prefers_longest_reference(
    message: str, expected_id: str, expected_target: str | None
) -> None:
    state = _state_with_starters()
    state["player"]["generated_abilities"][0]["display_name"] = "Grave"
    state["player"]["generated_abilities"][1]["display_name"] = "Grave Echo"
    original_state = copy.deepcopy(state)
    parsed = asyncio.run(ActionParserAgent().parse(
        message=message, campaign_state=json.dumps(state), recent_turns=[], deterministic_only=True
    ))
    assert parsed.action == ActionType.ABILITY_CHECK
    assert parsed.parameters == {"ability_id": expected_id}
    assert parsed.target == expected_target
    assert state == original_state


@pytest.mark.parametrize("deterministic_only", [True, False])
@pytest.mark.parametrize("message", ["use Grave Echo", "use Grave Echo on the brass key"])
def test_legacy_cross_reference_ambiguity_is_non_executable(
    monkeypatch: pytest.MonkeyPatch, deterministic_only: bool, message: str
) -> None:
    state = _state_with_starters()
    state["player"]["generated_abilities"][0].update(
        {"ability_id": "grave_echo", "display_name": "Veil Tremor"}
    )
    state["player"]["generated_abilities"][1].update(
        {"ability_id": "veil_tremor", "display_name": "Grave Echo"}
    )
    state["player"]["progression"]["unlocked_abilities"] = ["grave_echo", "veil_tremor"]
    original_state = copy.deepcopy(state)
    assert load_authoritative_campaign_state(json.dumps(state)) == state

    async def fake_generate_structured(**kwargs: object) -> ActionParserOutput:
        assert kwargs["response_model"] is ActionParserOutput
        return ActionParserOutput(
            action=ActionType.ABILITY_CHECK,
            parameters=ActionParserParameters(ability_id="grave_echo"),
            parse_status="ok",
            confidence=1,
        )

    monkeypatch.setattr(
        "app.agents.action_parser.model_client.generate_structured", fake_generate_structured
    )
    parsed = asyncio.run(ActionParserAgent().parse(
        message=message,
        campaign_state=json.dumps(state),
        recent_turns=[],
        deterministic_only=deterministic_only,
    ))
    assert parsed.action == ActionType.UNKNOWN
    assert parsed.parse_status == "ambiguous"
    assert parsed.parameters == {}
    assert parsed.target is None
    updated, result = ToolExecutor().execute(parsed_action=parsed, campaign_state=json.dumps(state))
    assert updated == original_state
    assert result.ability_result is None


@pytest.mark.parametrize(
    ("message", "model_ability_id", "model_target", "expected_action", "expected_target"),
    [
        ("do something useful", "whispering_touch", "brass key", ActionType.UNKNOWN, None),
        ("use Grave Echo", "whispering_touch", "brass key", ActionType.UNKNOWN, None),
        ("use Whispering Grasp", "invented_ability", "brass key", ActionType.UNKNOWN, None),
        ("use Whispering Grasp", None, "brass key", ActionType.UNKNOWN, None),
        ("use Whispering Grasp", "whispering_touch", "brass key", ActionType.ABILITY_CHECK, None),
        (
            "use Whispering Grasp on the brass key", "whispering_touch", "candle",
            ActionType.UNKNOWN, None,
        ),
        (
            "use Whispering Grasp on the brass key", "whispering_touch", "brass key",
            ActionType.ABILITY_CHECK, "brass key",
        ),
        (
            "use whispering_touch on the brass key", "whispering_touch", "BRASS KEY",
            ActionType.ABILITY_CHECK, "brass key",
        ),
        (
            "use Whispering Grasp on the brass key", "whispering_touch", None,
            ActionType.ABILITY_CHECK, "brass key",
        ),
    ],
)
def test_provider_ability_requests_require_explicit_authoritative_intent(
    monkeypatch: pytest.MonkeyPatch,
    message: str,
    model_ability_id: str | None,
    model_target: str | None,
    expected_action: ActionType,
    expected_target: str | None,
) -> None:
    state = _state_with_starters()
    state["items"]["brass_key"]["location"] = room_location("entry_hall")
    original_state = copy.deepcopy(state)
    calls = 0

    async def fake_generate_structured(**kwargs: object) -> ActionParserOutput:
        nonlocal calls
        calls += 1
        assert kwargs["response_model"] is ActionParserOutput
        return ActionParserOutput(
            action=ActionType.ABILITY_CHECK,
            target=model_target,
            parameters=ActionParserParameters(ability_id=model_ability_id),
            parse_status="ok",
            confidence=1,
        )

    monkeypatch.setattr(
        "app.agents.action_parser.model_client.generate_structured", fake_generate_structured
    )
    parsed = asyncio.run(ActionParserAgent().parse(
        message=message, campaign_state=json.dumps(state), recent_turns=[]
    ))
    assert calls == 1
    assert parsed.action == expected_action
    assert parsed.target == expected_target
    assert state == original_state
    updated, result = ToolExecutor().execute(parsed_action=parsed, campaign_state=json.dumps(state))
    if expected_target is None:
        assert updated == original_state
        assert result.state_delta == {}
        assert result.success is False
    else:
        assert result.success is True
        assert updated["items"]["brass_key"]["location"] == "player:current"


def test_provider_cannot_invoke_an_unowned_ability(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state_with_starters()
    state["player"]["progression"]["unlocked_abilities"].remove("whispering_touch")

    async def fake_generate_structured(**kwargs: object) -> ActionParserOutput:
        return ActionParserOutput(
            action=ActionType.ABILITY_CHECK,
            target="brass key",
            parameters=ActionParserParameters(ability_id="whispering_touch"),
            parse_status="ok",
            confidence=1,
        )

    monkeypatch.setattr(
        "app.agents.action_parser.model_client.generate_structured", fake_generate_structured
    )
    parsed = asyncio.run(ActionParserAgent().parse(
        message="use Whispering Grasp on the brass key", campaign_state=json.dumps(state), recent_turns=[]
    ))
    assert parsed.action == ActionType.UNKNOWN
    assert parsed.parse_status == "invalid"
    assert parsed.parameters == {}
    assert parsed.target is None


@pytest.mark.parametrize("parse_status", ["ok", "ambiguous", "invalid"])
def test_provider_matching_retains_common_item_disambiguation(
    monkeypatch: pytest.MonkeyPatch, parse_status: str
) -> None:
    state = _state_with_starters()
    state["player"]["generated_abilities"][1]["display_name"] = "Light"

    async def fake_generate_structured(**kwargs: object) -> ActionParserOutput:
        return ActionParserOutput.model_validate({
            "action": "ability_check",
            "target": "candle",
            "parameters": {"ability_id": "whispering_touch"},
            "parse_status": parse_status,
            "confidence": 1,
        })

    monkeypatch.setattr(
        "app.agents.action_parser.model_client.generate_structured", fake_generate_structured
    )
    item_request = asyncio.run(ActionParserAgent().parse(
        message="use light on candle", campaign_state=json.dumps(state), recent_turns=[]
    ))
    assert item_request.action == ActionType.UNKNOWN
    assert item_request.parameters == {}
    assert item_request.target is None

    ability_request = asyncio.run(ActionParserAgent().parse(
        message="use my ability Light on candle", campaign_state=json.dumps(state), recent_turns=[]
    ))
    if parse_status == "ok":
        assert ability_request.action == ActionType.ABILITY_CHECK
        assert ability_request.parameters == {"ability_id": "whispering_touch"}
        assert ability_request.target == "candle"
    else:
        assert ability_request.action == ActionType.UNKNOWN
        assert ability_request.parameters == {}
        assert ability_request.target is None


def test_provider_cannot_invoke_an_unavailable_keen_eye(monkeypatch: pytest.MonkeyPatch) -> None:
    state = build_fresh_campaign_state()
    ensure_character_progression_state(state)
    assert unlock_ability(state, "keen_eye").success

    async def fake_generate_structured(**kwargs: object) -> ActionParserOutput:
        return ActionParserOutput(
            action=ActionType.ABILITY_CHECK,
            parameters=ActionParserParameters(ability_id="keen_eye"),
            parse_status="ok",
            confidence=1,
        )

    monkeypatch.setattr(
        "app.agents.action_parser.model_client.generate_structured", fake_generate_structured
    )
    parsed = asyncio.run(ActionParserAgent().parse(
        message="use Keen Eye", campaign_state=json.dumps(state), recent_turns=[]
    ))
    assert parsed.action == ActionType.UNKNOWN
    assert parsed.parameters == {}


def test_generated_descriptions_use_mechanics_not_persisted_prose() -> None:
    state = _state_with_starters()
    definitions = state["player"]["generated_abilities"]
    definitions[0]["description"] = "See every hidden thing from any distance."
    definitions[1]["description"] = "Reveal hidden markings on a nearby object."
    persisted_descriptions = [definition["description"] for definition in definitions]

    projections = {item.ability_id: item for item in project_owned_abilities(state)}
    sensory = projections["echo_sense"].description
    utility = projections["whispering_touch"].description
    assert sensory == (
        "Sense nearby supernatural presence, including from directly adjacent spaces."
    )
    assert utility == "Draw a small portable object from this room into your hand."
    assert "hidden" not in sensory.lower()
    assert "reveal" not in utility.lower()
    assert [
        definition["description"] for definition in state["player"]["generated_abilities"]
    ] == persisted_descriptions

    resolved = resolve_gameplay_ability_check(state, "whispering_touch")
    assert resolved.description == utility

    parser_context = ActionParserAgent()._build_parser_context(json.dumps(state))
    parser_ability = next(
        ability for ability in parser_context.abilities
        if ability["ability_id"] == "whispering_touch"
    )
    assert parser_ability["description"] == utility
    assert parser_ability["name"] == "Whispering Grasp"
    assert parser_ability["available"] is True

    state["player"]["progression"]["unlocked_abilities"].remove("whispering_touch")
    parser_context = ActionParserAgent()._build_parser_context(json.dumps(state))
    assert all(
        ability["ability_id"] != "whispering_touch"
        for ability in parser_context.abilities
    )


def test_each_specific_mechanic_has_distinct_engine_owned_summary() -> None:
    state = _state_with_starters()
    variants = [
        _mechanics(
            effect=AbilityEffect.SENSE,
            domain=AbilityDomain.SURROUNDINGS,
            detail=AbilityDetail.LIMITED,
            range=0,
            sense_filter=AbilitySenseFilter.PRESENCE,
        ),
        _mechanics(
            effect=AbilityEffect.SENSE,
            domain=AbilityDomain.SURROUNDINGS,
            detail=AbilityDetail.LIMITED,
            range=1,
            sense_filter=AbilitySenseFilter.SUPERNATURAL_PRESENCE,
        ),
        _mechanics(
            effect=AbilityEffect.MOVE,
            domain=AbilityDomain.OBJECT,
            detail=AbilityDetail.PRACTICAL,
            range=0,
            object_motion=AbilityObjectMotion.TOWARD_PLAYER,
        ),
        _mechanics(
            effect=AbilityEffect.TOGGLE,
            domain=AbilityDomain.OBJECT,
            detail=AbilityDetail.PRACTICAL,
            range=0,
            object_state=AbilityObjectState.OPEN,
        ),
        _mechanics(
            effect=AbilityEffect.TOGGLE,
            domain=AbilityDomain.OBJECT,
            detail=AbilityDetail.PRACTICAL,
            range=0,
            object_state=AbilityObjectState.LIT,
        ),
    ]
    summaries = []
    for mechanics in variants:
        index = 0 if mechanics.effect == AbilityEffect.SENSE else 1
        _set_mechanics(state, index, mechanics)
        target_id = state["player"]["generated_abilities"][index]["ability_id"]
        summary = next(
            ability.description
            for ability in project_owned_abilities(state)
            if ability.ability_id == target_id
        )
        summaries.append(summary)

    assert summaries == [
        "Sense active presence in this room.",
        "Sense nearby supernatural presence, including from directly adjacent spaces.",
        "Draw a small portable object from this room into your hand.",
        "Open or close a nearby ordinary object that can already be opened.",
        "Light or extinguish a nearby object that can normally hold a flame.",
    ]
    assert len(set(summaries)) == 5


@pytest.mark.parametrize(
    ("sensory_detail", "utility_detail"),
    [
        (AbilityDetail.LIMITED, AbilityDetail.PRACTICAL),
        (AbilityDetail.PRACTICAL, AbilityDetail.PRACTICAL),
        (AbilityDetail.LIMITED, AbilityDetail.LIMITED),
        (AbilityDetail.PRACTICAL, AbilityDetail.LIMITED),
    ],
)
def test_legacy_generic_generated_ability_remains_valid_but_unsupported(
    sensory_detail: AbilityDetail,
    utility_detail: AbilityDetail,
) -> None:
    state = _state_with_starters()
    first, second = state["player"]["generated_abilities"]
    first["display_name"] = "Nearby Perception"
    second["display_name"] = "Nearby Object Convenience"
    first["description"] = "Existing sensory prose remains unchanged."
    second["description"] = "Existing utility prose remains unchanged."
    state["player"]["generated_abilities"][0]["mechanics"] = {
        "effect": "sense",
        "domain": "surroundings",
        "channel": "supernatural",
        "detail": sensory_detail.value,
        "range": 1,
        "requires": ["nearby"],
        "bypasses": [],
    }
    state["player"]["generated_abilities"][1]["mechanics"] = {
        "effect": "minor_utility",
        "domain": "object",
        "channel": "supernatural",
        "detail": utility_detail.value,
        "range": 1,
        "requires": ["nearby"],
        "bypasses": [],
    }
    original_state = copy.deepcopy(state)
    persisted_definitions = copy.deepcopy(state["player"]["generated_abilities"])
    loaded_state = load_authoritative_campaign_state(json.dumps(state))
    assert loaded_state["player"]["generated_abilities"] == persisted_definitions
    definitions = generated_ability_definitions(state)
    assert definitions[0].mechanics.sense_filter is None
    assert definitions[0].mechanics.detail == sensory_detail
    assert definitions[1].mechanics.detail == utility_detail
    projected = project_owned_abilities(state)
    assert projected[0].description == "Sense faint or unusual changes in nearby surroundings."
    assert projected[1].description == (
        "Exert a small practical supernatural influence on a nearby ordinary object."
    )
    assert [ability.display_name for ability in projected] == [
        "Nearby Perception",
        "Nearby Object Convenience",
    ]
    assert state == original_state
    assert [ability.display_name for ability in definitions] == [
        "Nearby Perception",
        "Nearby Object Convenience",
    ]
    assert [definition["description"] for definition in loaded_state["player"]["generated_abilities"]] == [
        "Existing sensory prose remains unchanged.",
        "Existing utility prose remains unchanged.",
    ]
    for ability in definitions:
        result = resolve_gameplay_ability_check(state, ability.ability_id)
        assert result.status == AbilityGameplayStatus.UNSUPPORTED
        assert result.error_code == "unsupported_generated_mechanic"


def test_presence_sensing_is_bounded_filtered_and_identity_free() -> None:
    state = _state_with_starters()
    state["player"]["location"] = "grand_corridor"
    state["npcs"] = {
        "local_person": {
            "location": "grand_corridor",
            "status": "active",
            "tags": ["human"],
            "name": "Local Person",
        },
        "near_ghost": {
            "location": "library",
            "status": "active",
            "tags": ["ghost", "undead"],
            "name": "Secret Ghost",
        },
        "inactive_ghost": {
            "location": "entry_hall",
            "status": "absent",
            "tags": ["ghost"],
            "name": "Inactive Ghost",
        },
        "distant_undead": {
            "location": "crypt",
            "status": "active",
            "tags": ["undead"],
            "name": "Distant Undead",
        },
    }
    _set_mechanics(
        state,
        0,
        _mechanics(
            effect=AbilityEffect.SENSE,
            domain=AbilityDomain.SURROUNDINGS,
            detail=AbilityDetail.LIMITED,
            range=1,
            sense_filter=AbilitySenseFilter.PRESENCE,
        ),
    )
    original_state = copy.deepcopy(state)
    _, presence_result = _execute_generated(state, "echo_sense")
    assert presence_result.ability_result is not None
    presence = presence_result.ability_result.presence_effect
    assert presence is not None
    assert presence.found is True
    assert presence.current_room_count == 1
    assert presence.adjacent_room_count == 1
    assert state == original_state

    supernatural_mechanics = _mechanics(
        effect=AbilityEffect.SENSE,
        domain=AbilityDomain.SURROUNDINGS,
        detail=AbilityDetail.LIMITED,
        range=1,
        sense_filter=AbilitySenseFilter.SUPERNATURAL_PRESENCE,
    )
    _set_mechanics(state, 0, supernatural_mechanics)
    original_state = copy.deepcopy(state)
    _, supernatural_result = _execute_generated(state, "echo_sense")
    assert supernatural_result.ability_result is not None
    supernatural = supernatural_result.ability_result.presence_effect
    assert supernatural is not None
    assert supernatural.found is True
    assert supernatural.current_room_count == 0
    assert supernatural.adjacent_room_count == 1
    assert "secret_ghost" not in supernatural_result.ability_result.model_dump_json()
    assert "Secret Ghost" not in supernatural_result.ability_result.model_dump_json()
    assert state == original_state


def test_presence_sensing_range_zero_and_no_result_are_authoritative() -> None:
    state = _state_with_starters()
    state["player"]["location"] = "entry_hall"
    state["npcs"] = {
        "adjacent_person": {
            "location": "grand_corridor",
            "status": "active",
            "tags": ["human"],
        },
        "absent_person": {
            "location": "entry_hall",
            "status": "absent",
            "tags": ["human"],
        },
    }
    _set_mechanics(
        state,
        0,
        _mechanics(
            effect=AbilityEffect.SENSE,
            domain=AbilityDomain.SURROUNDINGS,
            detail=AbilityDetail.LIMITED,
            range=0,
            sense_filter=AbilitySenseFilter.PRESENCE,
        ),
    )
    original_state = copy.deepcopy(state)
    _, result = _execute_generated(state, "echo_sense")

    assert result.ability_result is not None
    effect = result.ability_result.presence_effect
    assert effect is not None
    assert effect.found is False
    assert effect.current_room_count == 0
    assert effect.adjacent_room_count == 0
    assert result.state_delta == {}
    assert state == original_state


def test_generated_pull_moves_only_a_portable_current_room_item() -> None:
    state = _state_with_starters()
    state["items"]["brass_key"]["location"] = room_location("entry_hall")
    original_inventory = list(state["player"]["inventory"])

    updated, result = _execute_generated(state, "whispering_touch", "brass key")
    assert result.success is True
    assert result.ability_result is not None
    assert result.ability_result.status == AbilityGameplayStatus.RESOLVED
    assert result.state_delta == {
        "items": {
            "brass_key": {
                "location": {
                    "from": "room:entry_hall",
                    "to": "player:current",
                }
            }
        },
        "player": {
            "inventory": {
                "from": original_inventory,
                "to": updated["player"]["inventory"],
            }
        },
    }
    assert updated["items"]["brass_key"]["location"] == "player:current"
    assert "brass_key" in updated["player"]["inventory"]


@pytest.mark.parametrize(
    ("target", "item_location", "portable", "error_code"),
    [
        ("heavy statue", "room:entry_hall", False, "target_not_portable"),
        ("brass key", "room:library", True, "target_not_nearby"),
        ("missing token", "room:entry_hall", True, "target_missing"),
    ],
)
def test_generated_pull_rejections_do_not_mutate_state(
    target: str,
    item_location: str,
    portable: bool,
    error_code: str,
) -> None:
    state = _state_with_starters()
    if target == "heavy statue":
        state["items"]["heavy_statue"]["portable"] = portable
    elif target == "brass key":
        state["items"]["brass_key"]["location"] = item_location
    original_state = copy.deepcopy(state)

    updated, result = _execute_generated(state, "whispering_touch", target)
    assert result.success is False
    assert result.ability_result is not None
    assert result.ability_result.error_code == error_code
    assert result.state_delta == {}
    assert updated == original_state


def test_generated_pull_rejects_ambiguous_target_without_mutation() -> None:
    state = _state_with_starters()
    state["items"]["brass_key"]["location"] = room_location("entry_hall")
    state["items"]["bronze_key"] = {
        "id": "bronze_key",
        "name": "Bronze Key",
        "description": "",
        "location": room_location("entry_hall"),
        "portable": True,
        "quantity": 1,
        "aliases": [],
        "tags": ["key"],
        "properties": {},
    }
    original_state = copy.deepcopy(state)

    updated, result = _execute_generated(state, "whispering_touch", "key")
    assert result.success is False
    assert result.ability_result is not None
    assert result.ability_result.error_code == "target_ambiguous"
    assert updated == original_state


def test_generated_open_toggle_changes_only_existing_open_state() -> None:
    state = _state_with_starters()
    state["player"]["location"] = "library"
    _set_mechanics(
        state,
        1,
        _mechanics(
            effect=AbilityEffect.TOGGLE,
            domain=AbilityDomain.OBJECT,
            detail=AbilityDetail.PRACTICAL,
            range=0,
            object_state=AbilityObjectState.OPEN,
        ),
    )
    previous = copy.deepcopy(state["items"]["old_book"]["properties"])

    updated, result = _execute_generated(state, "whispering_touch", "old book")
    assert result.success is True
    assert result.state_delta == {
        "items": {
            "old_book": {
                "properties": {
                    "is_open": {"from": False, "to": True},
                }
            }
        }
    }
    assert updated["items"]["old_book"]["properties"]["is_open"] is True
    assert {
        key: value
        for key, value in updated["items"]["old_book"]["properties"].items()
        if key != "is_open"
    } == {key: value for key, value in previous.items() if key != "is_open"}


def test_generated_ability_execution_does_not_dispatch_tools_or_adjudicate() -> None:
    state = _state_with_starters()
    state["npcs"] = {}
    _, result = ToolExecutor(registry=ToolRegistry(mode="local")).execute(
        parsed_action=ParsedAction(
            raw_text="use grave echo",
            action=ActionType.ABILITY_CHECK,
            parameters={"ability_id": "echo_sense"},
            confidence=1,
            parse_status="ok",
        ),
        campaign_state=json.dumps(state),
    )
    assert result.success is True
    assert result.ability_result is not None
    assert result.ability_result.presence_effect is not None
    assert result.ability_result.presence_effect.found is False


def test_narrator_projection_exposes_only_authoritative_object_outcome() -> None:
    state = _state_with_starters()
    state["player"]["location"] = "library"
    _set_mechanics(
        state,
        1,
        _mechanics(
            effect=AbilityEffect.TOGGLE,
            domain=AbilityDomain.OBJECT,
            detail=AbilityDetail.PRACTICAL,
            range=0,
            object_state=AbilityObjectState.OPEN,
        ),
    )
    _, result = _execute_generated(state, "whispering_touch", "old book")
    assert result.ability_result is not None
    narrator_result = project_narrator_ability_gameplay_result(result.ability_result)
    assert narrator_result.effect_resolved is True
    assert narrator_result.object_effect is not None
    assert narrator_result.object_effect.item_name == "Old Book"
    assert narrator_result.object_effect.outcome.value == "opened"
    serialized = narrator_result.model_dump_json()
    for internal_detail in ("item_id", "toggle_open", "previous_value", "error_code", "reason"):
        assert internal_detail not in serialized


def test_generated_open_toggle_rejects_nonopenable_and_off_room_items() -> None:
    state = _state_with_starters()
    _set_mechanics(
        state,
        1,
        _mechanics(
            effect=AbilityEffect.TOGGLE,
            domain=AbilityDomain.OBJECT,
            detail=AbilityDetail.PRACTICAL,
            range=0,
            object_state=AbilityObjectState.OPEN,
        ),
    )
    state["items"]["brass_key"]["location"] = room_location("entry_hall")
    original_state = copy.deepcopy(state)
    updated, nonopenable = _execute_generated(state, "whispering_touch", "brass key")
    assert nonopenable.success is False
    assert nonopenable.ability_result is not None
    assert nonopenable.ability_result.error_code == "target_not_openable"
    assert updated == original_state
    assert "is_open" not in updated["items"]["brass_key"]["properties"]
    assert updated["items"]["brass_key"]["properties"]["opens"] == "cellar_door"

    state["items"]["brass_key"]["location"] = room_location("entry_hall")
    state["items"]["brass_key"]["properties"].update(
        {"openable": True, "is_open": False, "locked": True}
    )
    original_state = copy.deepcopy(state)
    updated, locked = _execute_generated(state, "whispering_touch", "brass key")
    assert locked.success is False
    assert locked.ability_result is not None
    assert locked.ability_result.error_code == "target_locked"
    assert updated == original_state

    state["player"]["location"] = "entry_hall"
    state["items"]["old_book"]["location"] = room_location("library")
    original_state = copy.deepcopy(state)
    updated, off_room = _execute_generated(state, "whispering_touch", "old book")
    assert off_room.success is False
    assert off_room.ability_result is not None
    assert off_room.ability_result.error_code == "target_not_nearby"
    assert updated == original_state


def test_generated_light_toggle_changes_only_lit_and_needs_no_ignition_item() -> None:
    state = _state_with_starters()
    state["player"]["location"] = "dining_room"
    _set_mechanics(
        state,
        1,
        _mechanics(
            effect=AbilityEffect.TOGGLE,
            domain=AbilityDomain.OBJECT,
            detail=AbilityDetail.PRACTICAL,
            range=0,
            object_state=AbilityObjectState.LIT,
        ),
    )
    previous = copy.deepcopy(state["items"]["candle"]["properties"])

    lit_state, lit_result = _execute_generated(state, "whispering_touch", "candle")
    assert lit_result.success is True
    assert lit_result.state_delta == {
        "items": {"candle": {"properties": {"lit": {"from": False, "to": True}}}}
    }
    assert lit_state["items"]["candle"]["properties"]["lit"] is True
    assert {
        key: value
        for key, value in lit_state["items"]["candle"]["properties"].items()
        if key != "lit"
    } == {key: value for key, value in previous.items() if key != "lit"}

    _, extinguished_result = _execute_generated(
        lit_state,
        "whispering_touch",
        "candle",
    )
    assert extinguished_result.success is True
    assert extinguished_result.state_delta["items"]["candle"]["properties"]["lit"] == {
        "from": True,
        "to": False,
    }


def test_generated_light_toggle_rejects_nonlightable_and_off_room_items() -> None:
    state = _state_with_starters()
    state["player"]["location"] = "dining_room"
    _set_mechanics(
        state,
        1,
        _mechanics(
            effect=AbilityEffect.TOGGLE,
            domain=AbilityDomain.OBJECT,
            detail=AbilityDetail.PRACTICAL,
            range=0,
            object_state=AbilityObjectState.LIT,
        ),
    )
    state["items"]["old_book"]["location"] = room_location("dining_room")
    original_state = copy.deepcopy(state)
    updated, nonlightable = _execute_generated(state, "whispering_touch", "old book")
    assert nonlightable.success is False
    assert nonlightable.ability_result is not None
    assert nonlightable.ability_result.error_code == "target_not_lightable"
    assert updated == original_state

    state["items"]["candle"]["location"] = room_location("library")
    original_state = copy.deepcopy(state)
    updated, off_room = _execute_generated(state, "whispering_touch", "candle")
    assert off_room.success is False
    assert off_room.ability_result is not None
    assert off_room.ability_result.error_code == "target_not_nearby"
    assert updated == original_state


def test_narrator_ability_projection_preserves_owned_but_unavailable_and_unknown() -> None:
    state = build_fresh_campaign_state()
    ensure_character_progression_state(state)
    state["player"]["progression"]["tracks"]["investigation"] = 1
    assert unlock_ability(state, "keen_eye").success

    owned_unavailable = resolve_gameplay_ability_check(state, "keen_eye")
    known_unowned = resolve_gameplay_ability_check(build_fresh_campaign_state(), "keen_eye")
    unknown = resolve_gameplay_ability_check(state, "unknown_ability")

    assert owned_unavailable.owned is True
    assert owned_unavailable.available is False
    assert known_unowned.owned is False
    assert known_unowned.available is False
    assert unknown.owned is False
    assert unknown.available is False

    owned_projection = project_narrator_ability_gameplay_result(owned_unavailable)
    known_unowned_projection = project_narrator_ability_gameplay_result(known_unowned)
    unknown_projection = project_narrator_ability_gameplay_result(unknown)

    assert owned_projection.owned is True
    assert owned_projection.available is False
    assert known_unowned_projection.owned is False
    assert known_unowned_projection.available is False
    assert unknown_projection.owned is False
    assert unknown_projection.available is False
    assert unknown_projection.display_name is None
    for projection in (owned_projection, known_unowned_projection, unknown_projection):
        dumped = projection.model_dump_json()
        assert "error_code" not in dumped
        assert "reason" not in dumped


def test_narrator_receives_player_safe_generated_ability_outcome(monkeypatch) -> None:
    state = _state_with_starters()
    state["player"]["generated_abilities"][0]["description"] = (
        "Read minds and reveal hidden things anywhere."
    )
    state["npcs"] = {
        "secret_wraith": {
            "id": "secret_wraith",
            "name": "Hidden Wraith",
            "description": "A forgotten spirit.",
            "location": "grand_corridor",
            "status": "active",
            "tags": ["undead"],
        }
    }
    state["player"]["location"] = "entry_hall"
    normalized_state = load_authoritative_campaign_state(json.dumps(state))
    updated_state, tool_result = ToolExecutor().execute(
        parsed_action=ParsedAction(
            raw_text="use grave echo",
            action=ActionType.ABILITY_CHECK,
            parameters={"ability_id": "echo_sense"},
            confidence=1,
            parse_status="ok",
        ),
        campaign_state=json.dumps(state),
    )

    assert updated_state == normalized_state
    assert tool_result.ability_result is not None
    assert tool_result.ability_result.status == AbilityGameplayStatus.RESOLVED
    assert tool_result.ability_result.presence_effect is not None
    assert tool_result.ability_result.presence_effect.found is True
    assert tool_result.ability_result.presence_effect.current_room_count == 0
    assert tool_result.ability_result.presence_effect.adjacent_room_count == 1

    captured_messages = []

    async def fake_generate_text(*, messages, **kwargs):  # noqa: ANN202, ARG001
        captured_messages.extend(messages)
        return "The faint echo fades without revealing anything."

    monkeypatch.setattr("app.agents.narrator.model_client.generate_text", fake_generate_text)
    asyncio.run(
        NarratorAgent().generate(
            payload=NarratorAgentInput(
                player_message="use echo sense",
                scene_context=build_narrator_scene_context(json.dumps(state)),
                parsed_action=ParsedAction(
                    raw_text="use grave echo",
                    action=ActionType.ABILITY_CHECK,
                    parameters={"ability_id": "echo_sense"},
                    confidence=1,
                    parse_status="ok",
                ),
                tool_result=tool_result,
            )
        )
    )

    request_context = "\n".join(
        message["content"] for message in captured_messages
        if isinstance(message.get("content"), str)
    ).lower()
    for implementation_term in (
        "unsupported_generated_mechanic",
        "unsupported mechanic",
        "unsupported_mechanic",
        "unimplemented",
        "implementation",
        "no deterministic gameplay rule",
        "no gameplay effect resolved",
        "gameplay",
        "mechanic",
        "engine",
        "internal rule",
        "schema",
        "payload",
    ):
        assert implementation_term not in request_context
    assert "grave echo" in request_context
    assert "sense nearby supernatural presence" in request_context
    assert "directly adjacent spaces" in request_context
    assert '"adjacent_room_count": 1' in request_context
    assert '"owned": true' in request_context
    assert '"available": true' in request_context
    assert '"effect_resolved": true' in request_context
    assert "secret_wraith" not in request_context
    assert "hidden wraith" not in request_context
    assert "read minds and reveal hidden things" not in request_context


def test_invalid_persisted_generated_definition_raises_campaign_state_error_everywhere() -> None:
    state = _state_with_starters()
    state["player"]["generated_abilities"][0]["mechanics"]["bypasses"] = ["darkness"]

    with pytest.raises(InvalidCampaignStateError, match="generated ability definitions"):
        resolve_gameplay_ability_check(state, "echo_sense")

    with pytest.raises(InvalidCampaignStateError, match="generated ability definitions"):
        ActionParserAgent()._build_parser_context(json.dumps(state))

    with pytest.raises(InvalidCampaignStateError, match="generated ability definitions"):
        ToolExecutor().execute(
            parsed_action=ParsedAction(
                raw_text="use echo sense",
                action=ActionType.ABILITY_CHECK,
                parameters={"ability_id": "echo_sense"},
                confidence=1,
                parse_status="ok",
            ),
            campaign_state=json.dumps(state),
        )
