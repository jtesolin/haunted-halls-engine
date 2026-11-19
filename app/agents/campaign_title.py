from __future__ import annotations

from typing import cast

from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel

from app.agents.base import BaseAgent
from app.ai.model_client import ModelCallResult, model_client
from app.guardrails.model_policy import ModelPolicy
from app.guardrails.token_budget import TokenBudget

_TITLE_PROMPT = (
    "Write a short campaign title (a few words) for a haunted-house text adventure, "
    "naming this specific story (its premise and objective), not the starting room or "
    "its furnishings. Reply with the title only: no quotes and no extra commentary."
)


class CampaignTitleInput(BaseModel):
    opening: str
    premise: str | None = None
    objective: str | None = None


class CampaignTitleOutput(BaseModel):
    title: str
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    total_tokens: int | None = None


class CampaignTitleGenerator(BaseAgent):
    @property
    def name(self) -> str:
        return "CampaignTitleGenerator"

    async def generate(
        self, *, payload: CampaignTitleInput, model: str | None = None
    ) -> CampaignTitleOutput:
        result = await model_client.generate_text(
            messages=self._build_messages(payload),
            model=model or ModelPolicy.campaign_title_model(),
            max_output_tokens=TokenBudget.campaign_title_max_output_tokens(),
            reasoning_effort=ModelPolicy.campaign_title_reasoning_effort(),
            timeout=15,
            return_usage=True,
        )
        if not isinstance(result, ModelCallResult):
            return CampaignTitleOutput(title=result)
        usage_fields = result.usage.as_output_fields() if result.usage is not None else {}
        return CampaignTitleOutput(title=result.output or "", **usage_fields)

    def _build_messages(self, payload: CampaignTitleInput) -> list[ChatCompletionMessageParam]:
        return cast(
            list[ChatCompletionMessageParam],
            [
                {"role": "developer", "content": _TITLE_PROMPT},
                {"role": "user", "content": self._user_content(payload)},
            ],
        )

    @staticmethod
    def _user_content(payload: CampaignTitleInput) -> str:
        story = "".join(
            f"{label}: {value}\n"
            for label, value in (("Premise", payload.premise), ("Objective", payload.objective))
            if value
        )
        return f"Write the campaign title.\n{story}Opening scene:\n{payload.opening}"
