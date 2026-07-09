from __future__ import annotations

import json
from typing import cast

from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, Field

from app.agents.base import BaseAgent
from app.ai.model_client import model_client
from app.guardrails.model_policy import ModelPolicy
from app.guardrails.token_budget import estimate_tokens


class MemorySummarizerInput(BaseModel):
    previous_summary: str | None = None
    recent_turns: list[dict[str, str]] = Field(default_factory=list)
    campaign_state: str
    latest_reply: str


class MemorySummarizerOutput(BaseModel):
    summary_text: str
    important_facts: list[str] = Field(default_factory=list)
    open_threads: list[str] = Field(default_factory=list)
    token_usage: int | None = None


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
    ) -> MemorySummarizerOutput:
        if ai_enabled:
            messages = self._build_messages(payload)
            summary_text = await model_client.generate_text(
                messages=messages,
                model=model or ModelPolicy.summarizer_model(),
                max_output_tokens=180,
                reasoning_effort="minimal",
                timeout=15,
            )
            summary = summary_text.strip()
            if summary:
                return MemorySummarizerOutput(
                    summary_text=summary,
                    token_usage=estimate_tokens(summary),
                )

        fallback_summary = self._build_fallback_summary(payload)
        return MemorySummarizerOutput(
            summary_text=fallback_summary,
            token_usage=estimate_tokens(fallback_summary),
        )

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
