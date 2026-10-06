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


def test_provider_request_prompt_allows_natural_language_ability_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state_with_available_utility()
    captured_messages = []

    async def capture_generate_structured(*, messages, response_model, **kwargs):
        captured_messages.extend(messages)
        assert response_model is ActionParserOutput
        return _ability_output()

    monkeypatch.setattr(
        "app.agents.action_parser.model_client.generate_structured",
        capture_generate_structured,
    )
    _parse("recall object on the statue", state)

    developer_prompt = next(
        message["content"]
        for message in captured_messages
        if message.get("role") == "developer"
    )
    assert "Interpret natural-language intent" in developer_prompt
    assert "Do not require a specific invocation verb or sentence pattern" in developer_prompt
    assert "Ability invocation uses `use`, `using`, `activate`, or `invoke`" not in developer_prompt
    assert "explicit target clauses use `on`, `toward`, or `at`" not in developer_prompt


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


@pytest.mark.parametrize(
    ("message", "model_target", "expected_action", "expected_target"),
    [
        ("Recall Object", "object", ActionType.UNKNOWN, None),
        ("Recall Object", "Recall Object", ActionType.UNKNOWN, None),
        ("Recall Object on the object", "object", ActionType.ABILITY_CHECK, "object"),
        ("Recall Object on Recall Object", "Recall Object", ActionType.ABILITY_CHECK, "Recall Object"),
        ("Recall-Object on (the object)!", "the object", ActionType.ABILITY_CHECK, "object"),
    ],
)
def test_ability_reference_span_cannot_also_ground_target(
    monkeypatch: pytest.MonkeyPatch,
    message: str,
    model_target: str,
    expected_action: ActionType,
    expected_target: str | None,
) -> None:
    state = _state_with_available_utility()
    _install_output(monkeypatch, _ability_output(target=model_target))

    result = _parse(message, state)

    assert result.action == expected_action
    assert result.target == expected_target
    if expected_action == ActionType.UNKNOWN:
        assert result.parse_status == "invalid"


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


@pytest.mark.parametrize(
    "message",
    [
        "use the Brass Key ability on candle",
        "try my Brass Key ability on candle",
        "use the ability Brass Key on candle",
        "use my ability Brass Key on candle",
    ],
)
def test_item_ability_collision_accepts_structurally_qualified_namespace(
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    state = _state_with_available_utility()
    state["player"]["generated_abilities"][1]["display_name"] = "Brass Key"
    state["items"]["brass_key"]["location"] = "room:entry_hall"
    _install_output(monkeypatch, _ability_output(target="candle"))

    result = _parse(message, state)

    assert result.action == ActionType.ABILITY_CHECK
    assert result.parameters == {"ability_id": "recall_object"}
    assert result.target == "candle"


@pytest.mark.parametrize(
    "message",
    [
        "use brass key on candle",
        "I lack the ability to use brass key on candle",
        "my ability failed; use brass key on candle",
        "I have an ability and use brass key on candle",
    ],
)
def test_item_ability_collision_rejects_unrelated_ability_qualifier(
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    state = _state_with_available_utility()
    state["player"]["generated_abilities"][1]["display_name"] = "Brass Key"
    state["items"]["brass_key"]["location"] = "room:entry_hall"
    _install_output(monkeypatch, _ability_output(target="candle"))

    result = _parse(message, state)

    assert result.action == ActionType.UNKNOWN
    assert result.parameters == {}


@pytest.mark.parametrize("legacy_ambiguity", [False, True])
def test_unique_ability_id_grounds_invocation_when_display_name_is_target(
    monkeypatch: pytest.MonkeyPatch,
    legacy_ambiguity: bool,
) -> None:
    state = _state_with_available_utility()
    state["player"]["generated_abilities"][1]["ability_id"] = "spectral_pull"
    state["player"]["generated_abilities"][1]["display_name"] = "Brass Key"
    state["player"]["progression"]["unlocked_abilities"].remove("recall_object")
    assert unlock_ability(state, "spectral_pull").success
    if legacy_ambiguity:
        state["player"]["generated_abilities"][0]["ability_id"] = "brass_key"
        state["player"]["progression"]["unlocked_abilities"].remove("echo_sense")
        assert unlock_ability(state, "brass_key").success
    state["items"]["brass_key"]["location"] = "room:entry_hall"
    _install_output(monkeypatch, _ability_output("spectral_pull", "brass key"))

    result = _parse("use spectral pull on brass key", state)

    assert result.action == ActionType.ABILITY_CHECK
    assert result.parse_status == "ok"
    assert result.parameters == {"ability_id": "spectral_pull"}
    assert result.target == "brass key"


@pytest.mark.parametrize(
    "message",
    [
        "use my Brass Key ability on Brass Key",
        "use the ability Brass Key on Brass Key",
        "Brass Key, target my Brass Key ability",
    ],
)
def test_qualified_ability_occurrence_and_same_name_target_are_grounded_independently(
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    state = _state_with_available_utility()
    state["player"]["generated_abilities"][1]["display_name"] = "Brass Key"
    state["items"]["brass_key"]["location"] = "room:entry_hall"
    _install_output(monkeypatch, _ability_output(target="Brass Key"))

    result = _parse(message, state)

    assert result.action == ActionType.ABILITY_CHECK
    assert result.parse_status == "ok"
    assert result.target == "Brass Key"


def test_namespace_qualification_cannot_be_borrowed_from_another_occurrence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state_with_available_utility()
    state["player"]["generated_abilities"][1]["display_name"] = "Brass Key"
    state["items"]["brass_key"]["location"] = "room:entry_hall"
    _install_output(monkeypatch, _ability_output(target="my Brass Key ability"))

    result = _parse("use Brass Key on my Brass Key ability", state)

    assert result.action == ActionType.UNKNOWN
    assert result.parse_status == "invalid"
    assert result.target is None


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
