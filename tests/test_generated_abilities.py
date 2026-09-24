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
from app.schemas.chat import ActionType, ParsedAction
from app.schemas.generated_abilities import AbilityGameplayStatus
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

    unsupported = generation.abilities[0].model_copy(
        update={"mechanics": generation.abilities[0].mechanics.model_copy(update={"bypasses": ("darkness",)})}
    )
    with pytest.raises(ValueError, match="bypass"):
        validate_starter_ability_definitions((unsupported, generation.abilities[1]))


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
