from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI

from app.agents import starter_abilities as starter_abilities_module
from app.agents.starter_abilities import StarterAbilityGenerator
from app.ai.model_client import ModelCallResult, ModelClient, ModelUsage
from app.game.abilities import validate_starter_ability_definitions
from app.schemas.generated_abilities import (
    AbilityEffect,
    AbilityObjectState,
    AbilitySenseFilter,
)
from app.schemas.starter_ability_provider import StarterUtilityOperation

_REAL_STARTER_GENERATE = StarterAbilityGenerator.generate


@dataclass
class MockedOpenAISDK:
    requests: list[dict[str, Any]] = field(default_factory=list)
    responses: deque[dict[str, Any]] = field(default_factory=deque)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/responses"
        assert self.responses, "Unexpected OpenAI request without a canned response."
        self.requests.append(json.loads(request.content))
        return httpx.Response(200, json=self.responses.popleft())


@pytest.fixture
def mocked_openai_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[MockedOpenAISDK]:
    transport_state = MockedOpenAISDK()
    client = AsyncOpenAI(
        api_key="test-only",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(transport_state.handle_request)
        ),
    )
    real_model_client = ModelClient()
    real_model_client._client = client
    monkeypatch.setattr(starter_abilities_module, "model_client", real_model_client)
    monkeypatch.setattr(StarterAbilityGenerator, "generate", _REAL_STARTER_GENERATE)
    try:
        yield transport_state
    finally:
        asyncio.run(client.close())


def _raw_responses_api_json(provider_output: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "resp_starter_contract_test",
        "object": "response",
        "created_at": 1791027000,
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "max_output_tokens": None,
        "model": "gpt-4.1-mini",
        "output": [
            {
                "id": "msg_starter_contract_test",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(provider_output),
                        "annotations": [],
                    }
                ],
            }
        ],
        "parallel_tool_calls": True,
        "temperature": 1.0,
        "tool_choice": "auto",
        "tools": [],
        "top_p": 1.0,
        "truncation": "disabled",
        "usage": {
            "input_tokens": 138,
            "input_tokens_details": {
                "cached_tokens": 12,
                "cache_write_tokens": 4,
            },
            "output_tokens": 52,
            "output_tokens_details": {"reasoning_tokens": 19},
            "total_tokens": 190,
        },
    }


def _provider_output(
    operation: StarterUtilityOperation,
    sense_filter: AbilitySenseFilter,
    sensory_range: int,
) -> dict[str, Any]:
    utility_names = {
        StarterUtilityOperation.RETRIEVE: ("Whispering Grasp", "Draw a small nearby object."),
        StarterUtilityOperation.TOGGLE_OPEN: ("Veiled Passage", "Shift a nearby openable object."),
        StarterUtilityOperation.TOGGLE_LIT: ("Lantern's Breath", "Change a nearby lightable object."),
    }
    display_name, description = utility_names[operation]
    return {
        "sensory_ability": {
            "ability_id": "grave_echo",
            "display_name": "Grave Echo",
            "description": "Sense nearby presence as a faint chill.",
            "track": "investigation",
            "sense_filter": sense_filter.value,
            "range": sensory_range,
        },
        "utility_ability": {
            "ability_id": f"starter_{operation.value}",
            "display_name": display_name,
            "description": description,
            "track": "occult",
            "operation": operation.value,
        },
    }


def _resolve_schema_reference(
    schema: dict[str, Any], root: dict[str, Any]
) -> dict[str, Any]:
    reference = schema.get("$ref")
    if not isinstance(reference, str):
        return schema
    prefix = "#/$defs/"
    assert reference.startswith(prefix)
    resolved: Any = root
    for part in reference[len("#/"):].split("/"):
        resolved = resolved[part]
    assert isinstance(resolved, dict)
    return resolved


def _assert_provider_schema_is_minimal_and_closed(
    request_body: dict[str, Any],
) -> None:
    response_format = request_body["text"]["format"]
    assert response_format["type"] == "json_schema"
    assert response_format["strict"] is True
    schema = response_format["schema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"sensory_ability", "utility_ability"}
    assert set(schema["required"]) == {"sensory_ability", "utility_ability"}

    sensory = _resolve_schema_reference(schema["properties"]["sensory_ability"], schema)
    utility = _resolve_schema_reference(schema["properties"]["utility_ability"], schema)
    sensory_fields = {
        "ability_id",
        "display_name",
        "description",
        "track",
        "sense_filter",
        "range",
    }
    utility_fields = {
        "ability_id",
        "display_name",
        "description",
        "track",
        "operation",
    }
    assert sensory["type"] == utility["type"] == "object"
    assert sensory["additionalProperties"] is utility["additionalProperties"] is False
    assert set(sensory["properties"]) == sensory_fields
    assert set(sensory["required"]) == sensory_fields
    assert set(utility["properties"]) == utility_fields
    assert set(utility["required"]) == utility_fields

    operation_schema = _resolve_schema_reference(
        utility["properties"]["operation"], schema
    )
    assert set(operation_schema["enum"]) == {
        StarterUtilityOperation.RETRIEVE.value,
        StarterUtilityOperation.TOGGLE_OPEN.value,
        StarterUtilityOperation.TOGGLE_LIT.value,
    }
    sense_schema = _resolve_schema_reference(
        sensory["properties"]["sense_filter"], schema
    )
    assert set(sense_schema["enum"]) == {
        AbilitySenseFilter.PRESENCE.value,
        AbilitySenseFilter.SUPERNATURAL_PRESENCE.value,
    }
    assert set(sensory["properties"]["range"]["enum"]) == {0, 1}

    serialized_schema = json.dumps(schema)
    for unsupported in (
        "effect",
        "domain",
        "object_motion",
        "object_state",
        "bypasses",
        "minor_utility",
    ):
        assert unsupported not in serialized_schema


@pytest.mark.parametrize(
    ("operation", "sense_filter", "sensory_range", "expected_effect", "object_state"),
    [
        (
            StarterUtilityOperation.RETRIEVE,
            AbilitySenseFilter.PRESENCE,
            0,
            AbilityEffect.MOVE,
            None,
        ),
        (
            StarterUtilityOperation.TOGGLE_OPEN,
            AbilitySenseFilter.SUPERNATURAL_PRESENCE,
            1,
            AbilityEffect.TOGGLE,
            AbilityObjectState.OPEN,
        ),
        (
            StarterUtilityOperation.TOGGLE_LIT,
            AbilitySenseFilter.PRESENCE,
            1,
            AbilityEffect.TOGGLE,
            AbilityObjectState.LIT,
        ),
    ],
)
def test_real_openai_sdk_contract_converts_all_supported_operations(
    mocked_openai_sdk: MockedOpenAISDK,
    operation: StarterUtilityOperation,
    sense_filter: AbilitySenseFilter,
    sensory_range: int,
    expected_effect: AbilityEffect,
    object_state: AbilityObjectState | None,
) -> None:
    mocked_openai_sdk.responses.append(
        _raw_responses_api_json(
            _provider_output(operation, sense_filter, sensory_range)
        )
    )

    result = asyncio.run(
        StarterAbilityGenerator().generate(
            provider_model_enabled=True,
            return_usage=True,
        )
    )

    assert isinstance(result, ModelCallResult)
    assert result.status == "completed"
    assert result.incomplete_details_reason is None
    assert result.usage == ModelUsage(
        input_tokens=138,
        cached_input_tokens=12,
        cache_write_input_tokens=4,
        output_tokens=52,
        reasoning_output_tokens=19,
        total_tokens=190,
    )
    assert result.output is not None
    validated = validate_starter_ability_definitions(result.output.abilities)
    sensory, utility = validated
    assert sensory.kind.value == "sensory"
    assert sensory.mechanics.sense_filter == sense_filter
    assert sensory.mechanics.range == sensory_range
    assert sensory.mechanics.effect == AbilityEffect.SENSE
    assert sensory.mechanics.object_motion is None
    assert sensory.mechanics.object_state is None
    assert sensory.mechanics.bypasses == ()
    assert sensory.minimum_points == utility.minimum_points == 0
    assert utility.kind.value == "utility"
    assert utility.mechanics.effect == expected_effect
    assert utility.mechanics.object_state == object_state
    assert utility.mechanics.sense_filter is None
    assert utility.mechanics.bypasses == ()
    if operation == StarterUtilityOperation.RETRIEVE:
        assert utility.mechanics.object_motion is not None
        assert utility.mechanics.object_motion.value == "toward_player"
        assert utility.mechanics.range == 0
    else:
        assert utility.mechanics.object_motion is None
        assert utility.mechanics.range == 0

    assert len(mocked_openai_sdk.requests) == 1
    request = mocked_openai_sdk.requests[0]
    assert isinstance(request["model"], str)
    _assert_provider_schema_is_minimal_and_closed(request)
