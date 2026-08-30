from __future__ import annotations

import json
from typing import cast

from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, Field

from app.agents.base import BaseAgent
from app.ai.model_client import ModelCallResult, model_client
from app.guardrails.model_policy import ModelPolicy
from app.guardrails.token_budget import TokenBudget


class MemorySummarizerInput(BaseModel):
    previous_summary: str | None = None
    recent_turns: list[dict[str, str]] = Field(default_factory=list)
    campaign_state: str
    latest_reply: str


class MemorySummarizerOutput(BaseModel):
    summary_text: str
    important_facts: list[str] = Field(default_factory=list)
    open_threads: list[str] = Field(default_factory=list)
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    total_tokens: int | None = None


class MemorySummarizerAgent(BaseAgent):
    @property
    def name(self) -> str:
        return "MemorySummarizer"

    async def summarize(
        self,
        *,
        payload: MemorySummarizerInput,
        model: str | None = None,
        ai_enabled: bool,
        provider_model_enabled: bool = False,
    ) -> MemorySummarizerOutput:
        if provider_model_enabled:
            messages = self._build_messages(payload)
            result = await model_client.generate_text(
                messages=messages,
                model=model or ModelPolicy.summarizer_model(),
                max_output_tokens=TokenBudget.summarizer_max_output_tokens(),
                reasoning_effort="minimal",
                timeout=15,
                return_usage=True,
            )
            usage = result.usage if isinstance(result, ModelCallResult) else None
            summary_text = result.output if isinstance(result, ModelCallResult) else result
            summary = (summary_text or "").strip()
            if summary:
                if usage is not None:
                    return MemorySummarizerOutput(
                        summary_text=summary,
                        input_tokens=usage.input_tokens,
                        cached_input_tokens=usage.cached_input_tokens,
                        cache_write_input_tokens=usage.cache_write_input_tokens,
                        output_tokens=usage.output_tokens,
                        reasoning_output_tokens=usage.reasoning_output_tokens,
                        total_tokens=usage.total_tokens,
                    )
                return MemorySummarizerOutput(summary_text=summary)

        fallback_summary = self._build_fallback_summary(payload)
        return MemorySummarizerOutput(summary_text=fallback_summary)

    def _build_messages(self, payload: MemorySummarizerInput) -> list[ChatCompletionMessageParam]:
        messages = [
            {
                "role": "developer",
                "content": (
                    "Summarize the campaign in 2-4 sentences. Preserve long-term facts, unresolved goals, "
                    "and important state changes. Return plain text only."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Existing summary:\n{payload.previous_summary or 'None yet.'}\n\n"
                    f"Current state:\n{payload.campaign_state}\n\n"
                    f"Recent turns:\n{json.dumps(payload.recent_turns[-8:])}\n\n"
                    f"Latest reply:\n{payload.latest_reply}"
                ),
            },
        ]
        return cast(list[ChatCompletionMessageParam], messages)

    def _build_fallback_summary(self, payload: MemorySummarizerInput) -> str:
        snippets = [turn.get("content", "") for turn in payload.recent_turns[-4:] if turn.get("content")]
        if payload.previous_summary:
            snippets.insert(0, payload.previous_summary)
        snippets.append(payload.latest_reply)
        return "Memory summary: " + " | ".join(snippet for snippet in snippets if snippet)
