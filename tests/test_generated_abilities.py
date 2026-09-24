from __future__ import annotations

import asyncio
import copy
import json

import pytest

from app.agents.action_parser import ActionParserAgent
from app.agents.starter_abilities import StarterAbilityGenerator
from app.game.abilities import (
    evaluate_ability_availability,
    resolve_gameplay_ability_check,
    validate_starter_ability_definitions,
)
from app.game.campaign_state import build_fresh_campaign_state
from app.game.character_progression import ensure_character_progression_state, unlock_ability
from app.schemas.abilities import AbilityCheckOutcome, AbilityCheckResult
from app.schemas.character_progression import ProgressionTrackId
from app.schemas.chat import ActionType, ParsedAction
from app.schemas.generated_abilities import AbilityGameplayResult, AbilityGameplayStatus
from app.services import tool_executor as tool_executor_module
from app.services.tool_executor import ToolExecutor


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


def test_provider_disabled_starters_are_valid_distinct_and_available() -> None:
    generation = asyncio.run(
        StarterAbilityGenerator().generate(provider_model_enabled=False)
    )
    abilities = validate_starter_ability_definitions(generation.abilities)
    state = _state_with_starters()

    assert len(abilities) == 2
    assert {ability.kind.value for ability in abilities} == {"sensory", "utility"}
    assert len({ability.ability_id for ability in abilities}) == 2
    assert all(evaluate_ability_availability(state, ability.ability_id).available for ability in abilities)
    assert "keen_eye" not in state["player"]["progression"]["unlocked_abilities"]


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


def test_blank_generated_display_text_is_rejected() -> None:
    generation = StarterAbilityGenerator()._stub_generation()

    blank_name = generation.abilities[0].model_copy(update={"display_name": "   "})
    with pytest.raises(ValueError, match="display_name"):
        validate_starter_ability_definitions((blank_name, generation.abilities[1]))

    blank_description = generation.abilities[0].model_copy(update={"description": "\t\n"})
    with pytest.raises(ValueError, match="description"):
        validate_starter_ability_definitions((blank_description, generation.abilities[1]))


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


def test_resolved_failed_ability_check_keeps_outer_success_false(monkeypatch) -> None:
    state = build_fresh_campaign_state()

    def failed_keen_eye_check(state, ability_id):  # noqa: ANN001, ARG001, ANN202
        return AbilityGameplayResult(
            ability_id="keen_eye",
            display_name="Keen Eye",
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
    assert tool_result.ability_result is not None
    assert tool_result.ability_result.status == AbilityGameplayStatus.RESOLVED
    assert tool_result.ability_result.check_result is not None
    assert tool_result.ability_result.check_result.success is False


def test_generated_ability_matching_requires_bounded_explicit_reference() -> None:
    state = _state_with_starters()
    generated = StarterAbilityGenerator()._stub_generation()
    light_ability = generated.abilities[0].model_copy(
        update={"ability_id": "ghost_light", "display_name": "Light"}
    )
    state["player"]["generated_abilities"] = [
        light_ability.model_dump(mode="json"),
        generated.abilities[1].model_dump(mode="json"),
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
            message="use light on the candle",
            campaign_state=json.dumps(state),
            recent_turns=[],
            deterministic_only=True,
        )
    )
    assert item_use_with_target.action == ActionType.USE
    assert item_use_with_target.target == "candle"
    assert item_use_with_target.parameters == {"with_item": "light"}

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


def test_generated_ability_is_known_but_not_yet_executable() -> None:
    state = _state_with_starters()
    result = resolve_gameplay_ability_check(state, "echo_sense")

    assert result.status == AbilityGameplayStatus.UNSUPPORTED
    assert result.error_code == "unsupported_generated_mechanic"


def test_invalid_persisted_generated_definition_fails_explicitly() -> None:
    state = _state_with_starters()
    state["player"]["generated_abilities"][0]["mechanics"]["bypasses"] = ["darkness"]

    with pytest.raises(ValueError, match="Persisted generated ability definition is invalid"):
        resolve_gameplay_ability_check(state, "echo_sense")
