import asyncio
from typing import Any

from app.agents.action_parser import ActionParserAgent
from app.agents.director import DirectorAgent, DirectorProposalResponse
from app.agents.memory_reflection import MemoryReflectionAgent, MemoryReflectionInput
from app.agents.memory_summarizer import MemorySummarizerAgent, MemorySummarizerInput
from app.agents.narrator import NarratorAgent, NarratorAgentInput
from app.game.campaign_state import build_fresh_campaign_state
from app.guardrails.model_policy import ModelPolicy
from app.schemas.chat import (
    ActionParserOutput,
    ActionType,
    NarratorRoom,
    NarratorSceneContext,
    ParsedAction,
    ToolExecutionResult,
)
from app.services.director_context import build_director_input


def test_model_policy_production_pairings() -> None:
    assert ModelPolicy.director_model() == "gpt-5.4-nano"
    assert ModelPolicy.director_reasoning_effort() == "none"

    assert ModelPolicy.memory_reflection_model() == "gpt-5.4-nano"
    assert ModelPolicy.memory_reflection_reasoning_effort() == "none"

    assert ModelPolicy.action_parser_model() == "gpt-5-nano"
    assert ModelPolicy.action_parser_reasoning_effort() == "minimal"

    assert ModelPolicy.summarizer_model() == "gpt-5-nano"
    assert ModelPolicy.summarizer_reasoning_effort() == "minimal"

    assert ModelPolicy.narrator_model() == "gpt-5-nano"
    assert ModelPolicy.narrator_reasoning_effort() == "medium"


def test_director_agent_passes_policy_reasoning_effort(monkeypatch: Any) -> None:
    captured_kwargs: dict[str, Any] = {}

    async def fake_generate_structured(*, messages: Any, **kwargs: Any) -> DirectorProposalResponse:
        captured_kwargs.update(kwargs)
        return DirectorProposalResponse.model_validate({"proposal": {"decision": "none"}})

    monkeypatch.setattr(
        "app.agents.director.model_client.generate_structured",
        fake_generate_structured,
    )

    director_input = build_director_input(
        build_fresh_campaign_state(),
        parsed_action=ParsedAction(
            raw_text="test",
            action=ActionType.OBSERVE,
            parse_status="ok",
        ),
        tool_result=ToolExecutionResult(
            success=True,
            summary="test",
        ),
    )

    asyncio.run(DirectorAgent().propose(director_input=director_input))

    assert captured_kwargs["reasoning_effort"] == "none"
    assert captured_kwargs["reasoning_effort"] == ModelPolicy.director_reasoning_effort()


def test_memory_reflection_agent_passes_policy_reasoning_effort(monkeypatch: Any) -> None:
    captured_kwargs: dict[str, Any] = {}

    async def fake_generate_text(*, messages: Any, **kwargs: Any) -> str:
        captured_kwargs.update(kwargs)
        return '["fact1"]'

    monkeypatch.setattr(
        "app.agents.memory_reflection.model_client.generate_text",
        fake_generate_text,
    )

    payload = MemoryReflectionInput(
        recent_turns=[],
        campaign_state="test_state",
    )

    asyncio.run(
        MemoryReflectionAgent().reflect(
            payload=payload,
            ai_enabled=True,
            provider_model_enabled=True,
        )
    )

    assert captured_kwargs["reasoning_effort"] == "none"
    assert captured_kwargs["reasoning_effort"] == ModelPolicy.memory_reflection_reasoning_effort()


def test_action_parser_agent_passes_policy_reasoning_effort(monkeypatch: Any) -> None:
    captured_kwargs: dict[str, Any] = {}

    async def fake_generate_structured(*, messages: Any, **kwargs: Any) -> ActionParserOutput:
        captured_kwargs.update(kwargs)
        return ActionParserOutput(
            action=ActionType.OBSERVE,
            target=None,
            stealth=False,
            confidence=1.0,
            parser_notes="ok",
            parse_status="ok",
        )

    monkeypatch.setattr(
        "app.agents.action_parser.model_client.generate_structured",
        fake_generate_structured,
    )

    asyncio.run(
        ActionParserAgent().parse(
            message="look around",
            campaign_state="{}",
            recent_turns=[],
        )
    )

    assert captured_kwargs["reasoning_effort"] == "minimal"
    assert captured_kwargs["reasoning_effort"] == ModelPolicy.action_parser_reasoning_effort()


def test_memory_summarizer_agent_passes_policy_reasoning_effort(monkeypatch: Any) -> None:
    captured_kwargs: dict[str, Any] = {}

    async def fake_generate_text(*, messages: Any, **kwargs: Any) -> str:
        captured_kwargs.update(kwargs)
        return "Summary text"

    monkeypatch.setattr(
        "app.agents.memory_summarizer.model_client.generate_text",
        fake_generate_text,
    )

    payload = MemorySummarizerInput(
        campaign_state="test_state",
        latest_reply="latest reply",
    )

    asyncio.run(
        MemorySummarizerAgent().summarize(
            payload=payload,
            ai_enabled=True,
            provider_model_enabled=True,
        )
    )

    assert captured_kwargs["reasoning_effort"] == "minimal"
    assert captured_kwargs["reasoning_effort"] == ModelPolicy.summarizer_reasoning_effort()


def test_narrator_agent_passes_policy_reasoning_effort(monkeypatch: Any) -> None:
    captured_kwargs: dict[str, Any] = {}

    async def fake_generate_text(*, messages: Any, **kwargs: Any) -> str:
        captured_kwargs.update(kwargs)
        return "Narrative output"

    monkeypatch.setattr(
        "app.agents.narrator.model_client.generate_text",
        fake_generate_text,
    )

    payload = NarratorAgentInput(
        player_message="hello",
        scene_context=NarratorSceneContext(
            current_room=NarratorRoom(
                id="foyer",
                name="Foyer",
                description="An entryway.",
            )
        ),
    )

    asyncio.run(
        NarratorAgent().generate(
            payload=payload,
        )
    )

    assert captured_kwargs["reasoning_effort"] == "medium"
    assert captured_kwargs["reasoning_effort"] == ModelPolicy.narrator_reasoning_effort()

