from __future__ import annotations

import asyncio
import json

import pytest

from app.agents.action_parser import ActionParserAgent
from app.game.campaign_state import build_fresh_campaign_state
from app.game.character_progression import ensure_character_progression_state, unlock_ability
from app.schemas.chat import ActionParserOutput, ActionParserParameters, ActionType
from tests.factories import starter_ability_generation


def _state_with_available_utility() -> dict:
    state = build_fresh_campaign_state()
    generated = starter_ability_generation()
    generated.abilities[1].ability_id = "recall_object"
    generated.abilities[1].display_name = "Recall Object"
    state["player"]["generated_abilities"] = [
        ability.model_dump(mode="json") for ability in generated.abilities
    ]
    ensure_character_progression_state(state)
    assert unlock_ability(state, generated.abilities[0].ability_id).success
    assert unlock_ability(state, "recall_object").success
    return state


def _ability_output(
    ability_id: str = "recall_object",
    target: str | None = "statue",
) -> ActionParserOutput:
    return ActionParserOutput(
        action=ActionType.ABILITY_CHECK,
        target=target,
        parameters=ActionParserParameters(ability_id=ability_id),
        stealth=False,
        confidence=1,
        parse_status="ok",
    )


def _install_output(monkeypatch: pytest.MonkeyPatch, output: ActionParserOutput) -> None:
    async def fake_generate_structured(*, response_model, **kwargs):
        assert response_model is ActionParserOutput
        return output

    monkeypatch.setattr(
        "app.agents.action_parser.model_client.generate_structured",
        fake_generate_structured,
    )


def _parse(message: str, state: dict):
    return asyncio.run(
        ActionParserAgent().parse(
            message=message,
            campaign_state=json.dumps(state),
            recent_turns=[],
        )
    )


def test_action_parser_uses_structured_provider_output(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state_with_available_utility()
    output = ActionParserOutput(
        action=ActionType.MOVE,
        target="library",
        parameters=ActionParserParameters(),
        stealth=True,
        confidence=0.93,
        parse_status="ok",
    )
    _install_output(monkeypatch, output)

    result = _parse("I walk quietly into the library.", state)

    assert result.action == ActionType.MOVE
    assert result.target == "library"
    assert result.stealth is True
    assert result.parse_status == "ok"


@pytest.mark.parametrize(
    "message",
    [
        "recall object on the statue",
        "try Recall Object on the statue",
        "I want to Recall Object on the statue",
        "could I use Recall Object on the statue?",
        "hit the statue with Recall Object",
        "Recall Object, target the statue",
    ],
)
def test_ability_request_is_grounded_without_invocation_grammar(
    monkeypatch: pytest.MonkeyPatch, message: str
) -> None:
    state = _state_with_available_utility()
    _install_output(monkeypatch, _ability_output())

    result = _parse(message, state)

    assert result.parse_status == "ok"
    assert result.action == ActionType.ABILITY_CHECK
    assert result.parameters == {"ability_id": "recall_object"}
    assert result.target == "statue"


def test_model_cannot_invent_an_ability_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state_with_available_utility()
    _install_output(monkeypatch, _ability_output())

    result = _parse("move the statue", state)

    assert result.action == ActionType.UNKNOWN
    assert result.parse_status == "invalid"
    assert result.parameters == {}


def test_model_cannot_invent_a_required_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state_with_available_utility()
    _install_output(monkeypatch, _ability_output(target="statue"))

    result = _parse("use Recall Object", state)

    assert result.action == ActionType.UNKNOWN
    assert result.parse_status == "invalid"
    assert result.target is None


@pytest.mark.parametrize(
    ("message", "model_target", "expected_target"),
    [
        ("Recall-Object on (the statue)!", "statue", "statue"),
        ("recall_object at a statue", "the statue", "statue"),
        ("Recall Object on THE STATUE.", "STATUE", "STATUE"),
    ],
)
def test_ability_and_target_grounding_use_bounded_canonical_equivalence(
    monkeypatch: pytest.MonkeyPatch,
    message: str,
    model_target: str,
    expected_target: str,
) -> None:
    state = _state_with_available_utility()
    _install_output(monkeypatch, _ability_output(target=model_target))

    result = _parse(message, state)

    assert result.action == ActionType.ABILITY_CHECK
    assert result.target == expected_target


def test_ambiguous_canonical_ability_reference_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state_with_available_utility()
    state["player"]["generated_abilities"][0]["display_name"] = "Recall_Object"
    state["player"]["progression"]["tracks"]["investigation"] = 2
    assert unlock_ability(state, "keen_eye").success
    _install_output(monkeypatch, _ability_output())

    result = _parse("recall object on the statue", state)

    assert result.action == ActionType.UNKNOWN
    assert result.parse_status == "ambiguous"
    assert result.parameters == {}


def test_item_ability_collision_requires_explicit_ability_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state_with_available_utility()
    state["player"]["generated_abilities"][1]["display_name"] = "Brass Key"
    state["items"]["brass_key"]["location"] = "room:entry_hall"
    _install_output(monkeypatch, _ability_output(target="candle"))

    ambiguous = _parse("use brass key on candle", state)
    explicit = _parse("use the Brass Key ability on candle", state)

    assert ambiguous.action == ActionType.UNKNOWN
    assert ambiguous.parameters == {}
    assert explicit.action == ActionType.ABILITY_CHECK
    assert explicit.parameters == {"ability_id": "recall_object"}
    assert explicit.target == "candle"


def test_provider_item_action_is_not_converted_to_an_ability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state_with_available_utility()
    output = ActionParserOutput(
        action=ActionType.USE,
        target="candle",
        parameters=ActionParserParameters(with_item="brass key"),
        confidence=1,
        parse_status="ok",
    )
    _install_output(monkeypatch, output)

    result = _parse("use brass key on candle", state)

    assert result.action == ActionType.USE
    assert result.target == "candle"
    assert result.parameters == {"with_item": "brass key"}


def test_parser_provider_failure_is_not_replaced_by_a_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_provider(**kwargs):
        raise OSError("provider unavailable")

    monkeypatch.setattr(
        "app.agents.action_parser.model_client.generate_structured",
        fail_provider,
    )
    with pytest.raises(Exception, match="Action parser model call failed"):
        _parse("wait", _state_with_available_utility())


def test_no_client_provider_call_is_blocked_by_test_safety_boundary() -> None:
    from app.ai.model_client import model_client

    with pytest.raises(AssertionError, match="unexpected live model call"):
        asyncio.run(
            model_client.generate_text(
                messages=[{"role": "user", "content": "no network"}],
                reasoning_effort="minimal",
                model="test",
                max_output_tokens=10,
                timeout=1,
            )
        )
