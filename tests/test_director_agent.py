import asyncio
import copy
import logging
from typing import Any

import pytest
from pydantic import ValidationError

from app.agents.director import (
    DirectorAgent,
    DirectorProposalOutputError,
    DirectorProposalResponse,
    DirectorProviderError,
)
from app.ai.model_client import ModelCallResult, ModelUsage
from app.ai.prompts import director_prompt
from app.game.campaign_state import build_fresh_campaign_state
from app.guardrails.model_policy import ModelPolicy
from app.guardrails.token_budget import TokenBudget, estimate_tokens
from app.schemas.chat import ActionType, ParsedAction, ToolExecutionResult
from app.schemas.director import DirectorInput, NoActionProposal, WorldActionProposal
from app.schemas.world import (
    AdvanceClockWorldAction,
    MoveNpcWorldAction,
    RecordFactWorldAction,
    SetNpcStatusWorldAction,
    WorldAction,
)
from app.services.director_context import build_director_input
from app.services.world_authority import WorldAuthorityExecutor


def _find_schema_keywords(schema: object, keywords: set[str]) -> dict[str, int]:
    counts = dict.fromkeys(keywords, 0)

    def scan(value: object) -> None:
        if isinstance(value, dict):
            for key, child_value in value.items():
                if key in counts:
                    counts[key] += 1
                scan(child_value)
        elif isinstance(value, list):
            for child_value in value:
                scan(child_value)

    scan(schema)
    return counts


def _director_input(state: dict[str, Any] | None = None) -> DirectorInput:
    return build_director_input(
        state or build_fresh_campaign_state(),
        parsed_action=ParsedAction(
            raw_text="This raw player text must not be sent to the Director.",
            action=ActionType.OBSERVE,
            parse_status="ok",
        ),
        tool_result=ToolExecutionResult(
            success=True,
            summary="The player observes the room.",
            applied_tools=["observe"],
        ),
    )


def _proposal_response(proposal: dict[str, Any]) -> DirectorProposalResponse:
    return DirectorProposalResponse.model_validate({"proposal": proposal})


def test_director_provider_response_schema_avoids_provider_sensitive_keywords() -> None:
    schema = DirectorProposalResponse.model_json_schema()

    assert _find_schema_keywords(
        schema,
        {"oneOf", "minLength", "minimum", "maximum"},
    ) == {
        "oneOf": 0,
        "minLength": 0,
        "minimum": 0,
        "maximum": 0,
    }


def test_director_requires_typed_input() -> None:
    with pytest.raises(TypeError, match="typed DirectorInput"):
        DirectorAgent().build_provider_request(director_input="{}")  # type: ignore[arg-type]


def test_director_request_contains_only_bounded_projection(monkeypatch) -> None:
    agent = DirectorAgent()
    director_input = _director_input()
    captured_messages = []

    async def fake_generate_structured(*, messages, **kwargs):  # noqa: ANN202, ARG001
        captured_messages.extend(messages)
        return _proposal_response({"decision": "none"})

    monkeypatch.setattr(
        "app.agents.director.model_client.generate_structured",
        fake_generate_structured,
    )

    result = asyncio.run(agent.propose(director_input=director_input))

    assert isinstance(result.proposal, NoActionProposal)
    assert captured_messages == agent.build_provider_request(
        director_input=director_input
    )
    assert len(captured_messages) == 2
    assert captured_messages[0]["content"] == director_prompt
    assert captured_messages[1]["content"] == (
        "Authoritative Director input:\n"
        + director_input.model_dump_json(exclude_none=True, indent=2)
    )
    assert "This raw player text must not be sent to the Director." not in str(
        captured_messages
    )
    assert "inventory" not in str(captured_messages)


def test_director_returns_no_action_proposal_and_provider_usage(monkeypatch) -> None:
    usage = ModelUsage(
        input_tokens=24,
        cached_input_tokens=3,
        cache_write_input_tokens=2,
        output_tokens=7,
        reasoning_output_tokens=1,
        total_tokens=31,
    )

    async def fake_generate_structured(*, messages, **kwargs):  # noqa: ANN202, ARG001
        assert kwargs["response_model"] is DirectorProposalResponse
        return ModelCallResult(
            output=_proposal_response({"decision": "none"}),
            usage=usage,
        )

    monkeypatch.setattr(
        "app.agents.director.model_client.generate_structured",
        fake_generate_structured,
    )

    result = asyncio.run(DirectorAgent().propose(director_input=_director_input()))

    assert isinstance(result.proposal, NoActionProposal)
    assert result.usage == usage


@pytest.mark.parametrize(
    ("provider_action", "expected_world_action"),
    [
        (
            {
                "action": "move_npc",
                "npc_id": "old_caretaker",
                "destination_room_id": "grand_corridor",
            },
            MoveNpcWorldAction(
                npc_id="old_caretaker",
                destination_room_id="grand_corridor",
            ),
        ),
        (
            {
                "action": "set_npc_status",
                "npc_id": "old_caretaker",
                "status": "absent",
            },
            SetNpcStatusWorldAction(npc_id="old_caretaker", status="absent"),
        ),
        (
            {
                "action": "advance_clock",
                "ticks": 1,
            },
            AdvanceClockWorldAction(ticks=1),
        ),
        (
            {
                "action": "record_fact",
                "fact": "The bell rang once.",
            },
            RecordFactWorldAction(fact="The bell rang once."),
        ),
    ],
)
def test_director_provider_actions_adapt_to_authoritative_world_actions(
    monkeypatch,
    provider_action: dict[str, Any],
    expected_world_action: WorldAction,
) -> None:
    async def fake_generate_structured(*, messages, **kwargs):  # noqa: ANN202, ARG001
        return _proposal_response(
            {
                "decision": "act",
                "world_action": provider_action,
            }
        )

    monkeypatch.setattr(
        "app.agents.director.model_client.generate_structured",
        fake_generate_structured,
    )

    result = asyncio.run(DirectorAgent().propose(director_input=_director_input()))

    assert isinstance(result.proposal, WorldActionProposal)
    assert result.proposal.world_action == expected_world_action


@pytest.mark.parametrize(
    "world_action",
    [
        {"action": "spawn_npc", "npc_id": "new_npc"},
        {"action": "unsupported_action"},
    ],
)
def test_director_provider_wrapper_rejects_unsupported_actions(
    world_action: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        _proposal_response({"decision": "act", "world_action": world_action})


@pytest.mark.parametrize(
    "world_action",
    [
        {"action": "move_npc", "npc_id": "old_caretaker"},
        {"action": "move_npc", "destination_room_id": "grand_corridor"},
        {"action": "set_npc_status", "npc_id": "old_caretaker"},
        {"action": "set_npc_status", "status": "absent"},
        {"action": "advance_clock"},
        {"action": "record_fact"},
    ],
)
def test_director_provider_action_rejects_missing_required_fields(
    world_action: dict[str, Any],
) -> None:
    response = _proposal_response({"decision": "act", "world_action": world_action})

    with pytest.raises(ValueError):
        response.to_domain_proposal()


def test_director_provider_no_action_rejects_action_payload() -> None:
    with pytest.raises(ValidationError):
        _proposal_response(
            {
                "decision": "none",
                "world_action": {
                    "action": "advance_clock",
                    "ticks": 1,
                },
            }
        )


def test_director_provider_act_requires_action_payload() -> None:
    with pytest.raises(ValidationError):
        _proposal_response({"decision": "act"})


@pytest.mark.parametrize(
    "world_action",
    [
        {
            "action": "move_npc",
            "npc_id": "old_caretaker",
            "destination_room_id": "grand_corridor",
            "status": "absent",
        },
        {
            "action": "set_npc_status",
            "npc_id": "old_caretaker",
            "status": "absent",
            "destination_room_id": "grand_corridor",
        },
        {
            "action": "advance_clock",
            "ticks": 1,
            "npc_id": "old_caretaker",
        },
        {
            "action": "record_fact",
            "fact": "The bell rang once.",
            "ticks": 1,
        },
    ],
)
def test_director_provider_action_rejects_fields_from_other_actions(
    world_action: dict[str, Any],
) -> None:
    response = _proposal_response({"decision": "act", "world_action": world_action})

    with pytest.raises(ValueError):
        response.to_domain_proposal()


@pytest.mark.parametrize(
    "world_action",
    [
        {"action": "advance_clock", "ticks": 0},
        {"action": "advance_clock", "ticks": 11},
        {
            "action": "move_npc",
            "npc_id": "",
            "destination_room_id": "grand_corridor",
        },
        {
            "action": "move_npc",
            "npc_id": "old_caretaker",
            "destination_room_id": "",
        },
    ],
)
def test_director_provider_action_delegates_value_bounds_to_domain_models(
    world_action: dict[str, Any],
) -> None:
    response = _proposal_response({"decision": "act", "world_action": world_action})

    with pytest.raises(ValidationError):
        response.to_domain_proposal()


@pytest.mark.parametrize(
    ("model_override", "expected_model"),
    [
        (None, ModelPolicy.director_model()),
        ("director-test-model", "director-test-model"),
    ],
)
def test_director_uses_policy_model_or_explicit_override(
    monkeypatch,
    model_override: str | None,
    expected_model: str,
) -> None:
    captured_kwargs: dict[str, Any] = {}

    async def fake_generate_structured(*, messages, **kwargs):  # noqa: ANN202, ARG001
        captured_kwargs.update(kwargs)
        return _proposal_response({"decision": "none"})

    monkeypatch.setattr(
        "app.agents.director.model_client.generate_structured",
        fake_generate_structured,
    )

    asyncio.run(
        DirectorAgent().propose(
            director_input=_director_input(),
            model=model_override,
        )
    )

    assert captured_kwargs["model"] == expected_model
    assert captured_kwargs["max_output_tokens"] == (
        TokenBudget.director_max_output_tokens()
    )
    assert captured_kwargs["reasoning_effort"] == "none"
    assert captured_kwargs["return_usage"] is True


def test_director_token_estimate_is_deterministic_and_uses_provider_request() -> None:
    agent = DirectorAgent()
    director_input = _director_input()
    request = agent.build_provider_request(director_input=director_input)
    expected = 0
    for message in request:
        content = message.get("content")
        if isinstance(content, str):
            expected += estimate_tokens(content)

    assert agent.estimate_provider_input_tokens(director_input=director_input) == expected
    assert agent.estimate_provider_input_tokens(director_input=director_input) == expected


def test_director_wraps_and_logs_provider_failures(monkeypatch, caplog) -> None:
    async def fake_generate_structured(*, messages, **kwargs):  # noqa: ANN202, ARG001
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(
        "app.agents.director.model_client.generate_structured",
        fake_generate_structured,
    )
    caplog.set_level(logging.ERROR, logger="app.agents.director")
    logging.getLogger("app.agents.director").disabled = False

    with pytest.raises(DirectorProviderError, match="Director model call failed"):
        asyncio.run(DirectorAgent().propose(director_input=_director_input()))

    assert "director_structured_call_failed" in caplog.text
    assert "provider unavailable" in caplog.text
    assert "This raw player text must not be sent to the Director." not in caplog.text


@pytest.mark.parametrize("provider_output", [None, object()])
def test_director_rejects_missing_or_unparseable_provider_output(
    monkeypatch,
    provider_output: object | None,
) -> None:
    async def fake_generate_structured(*, messages, **kwargs):  # noqa: ANN202, ARG001
        return provider_output

    monkeypatch.setattr(
        "app.agents.director.model_client.generate_structured",
        fake_generate_structured,
    )

    with pytest.raises(DirectorProposalOutputError):
        asyncio.run(DirectorAgent().propose(director_input=_director_input()))


def test_director_does_not_mutate_context_or_execute_proposal(monkeypatch) -> None:
    state = build_fresh_campaign_state()
    original_state = copy.deepcopy(state)
    director_input = _director_input(state)
    original_input = director_input.model_copy(deep=True)

    def fail_if_called(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("WorldAuthorityExecutor.execute must not be called")

    async def fake_generate_structured(*, messages, **kwargs):  # noqa: ANN202, ARG001
        return _proposal_response(
            {
                "decision": "act",
                "world_action": {
                    "action": "advance_clock",
                    "ticks": 1,
                },
            }
        )

    monkeypatch.setattr(WorldAuthorityExecutor, "execute", fail_if_called)
    monkeypatch.setattr(
        "app.agents.director.model_client.generate_structured",
        fake_generate_structured,
    )

    result = asyncio.run(DirectorAgent().propose(director_input=director_input))

    assert isinstance(result.proposal, WorldActionProposal)
    assert director_input == original_input
    assert state == original_state
