from __future__ import annotations

import json
from typing import cast

from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, Field

from app.agents.base import BaseAgent
from app.ai.model_client import model_client
from app.guardrails.model_policy import ModelPolicy
from app.guardrails.token_budget import TokenBudget, estimate_tokens


class MemoryCandidate(BaseModel):
    text: str
    importance: float = 1.0
    memory_type: str = "reflection"
    tags: list[str] = Field(default_factory=list)


class MemoryReflectionInput(BaseModel):
    recent_turns: list[dict[str, str]] = Field(default_factory=list)
    campaign_state: str
    current_summary: str | None = None


class MemoryReflectionOutput(BaseModel):
    memories_to_store: list[MemoryCandidate] = Field(default_factory=list)
    token_usage: int | None = None


class MemoryReflectionAgent(BaseAgent):
    @property
    def name(self) -> str:
        return "MemoryReflection"

    async def reflect(
        self,
        *,
        payload: MemoryReflectionInput,
        model: str | None = None,
        ai_enabled: bool,
    ) -> MemoryReflectionOutput:
        if ai_enabled:
            messages = self._build_messages(payload)
            raw_facts = await model_client.generate_text(
                messages=messages,
                model=model or ModelPolicy.memory_reflection_model(),
                max_output_tokens=TokenBudget.memory_reflection_max_output_tokens(),
                reasoning_effort="minimal",
                timeout=15,
            )
            facts = self._parse_reflection_facts(raw_facts)
            if facts:
                return MemoryReflectionOutput(
                    memories_to_store=[
                        MemoryCandidate(text=fact, importance=1.0, memory_type="reflection") for fact in facts
                    ],
                    token_usage=estimate_tokens(raw_facts),
                )

        fallback_facts = self._fallback_reflection_facts(
            campaign_state=payload.campaign_state,
            recent_turns=payload.recent_turns,
        )
        return MemoryReflectionOutput(
            memories_to_store=[
                MemoryCandidate(text=fact, importance=1.0, memory_type="reflection") for fact in fallback_facts
            ],
            token_usage=estimate_tokens("\n".join(fallback_facts)),
        )

    def _build_messages(self, payload: MemoryReflectionInput) -> list[ChatCompletionMessageParam]:
        prompt = (
            "What important long-term facts should be remembered? Return a JSON array of short facts. "
            "Only include facts that are worth keeping across many future turns."
        )
        messages = [
            {
                "role": "developer",
                "content": prompt,
            },
            {
                "role": "user",
                "content": (
                    f"Campaign state:\n{payload.campaign_state}\n\n"
                    f"Current summary:\n{payload.current_summary or 'None yet.'}\n\n"
                    f"Recent turns:\n{json.dumps(payload.recent_turns[-8:])}"
                ),
            },
        ]
        return cast(list[ChatCompletionMessageParam], messages)

    def _parse_reflection_facts(self, raw_text: str) -> list[str]:
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError:
            return [line.strip("- ") for line in raw_text.splitlines() if line.strip()]

        if isinstance(data, str):
            candidate = data.strip()
            return [candidate] if candidate else []

        if not isinstance(data, list):
            return []

        facts: list[str] = []
        for item in data:
            if isinstance(item, str) and item.strip():
                facts.append(item.strip())
        return facts

    def _fallback_reflection_facts(self, *, campaign_state: str, recent_turns: list[dict[str, str]]) -> list[str]:
        facts: list[str] = []
        if campaign_state and campaign_state != "No campaign state yet.":
            facts.append(f"Current campaign state snapshot: {campaign_state}")
        for turn in recent_turns[-3:]:
            content = turn.get("content", "").strip()
            if content:
                facts.append(content)
        return facts[:3]
