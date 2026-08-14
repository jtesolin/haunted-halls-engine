from __future__ import annotations

from typing import cast

from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, Field

from app.agents.base import BaseAgent
from app.ai.model_client import model_client
from app.ai.prompts import narrator_prompt
from app.guardrails.model_policy import ModelPolicy
from app.guardrails.token_budget import TokenBudget
from app.schemas.chat import ParsedAction, ToolExecutionResult


class NarratorAgentInput(BaseModel):
    player_message: str
    campaign_state: str
    recent_turns: list[dict[str, str]] = Field(default_factory=list)
    campaign_summary: str | None = None
    relevant_memories: list[dict[str, str]] = Field(default_factory=list)
    parsed_action: ParsedAction | None = None
    tool_result: ToolExecutionResult | None = None


class NarratorAgentOutput(BaseModel):
    reply_text: str


class NarratorAgent(BaseAgent):
    @property
    def name(self) -> str:
        return "Narrator"

    async def generate(
        self,
        *,
        payload: NarratorAgentInput,
        model: str | None = None,
    ) -> NarratorAgentOutput:
        memory_context = list(payload.relevant_memories)
        if payload.campaign_summary:
            memory_context = [
                {
                    "role": "user",
                    "content": f"Campaign summary:\n{payload.campaign_summary}",
                },
                *memory_context,
            ]

        messages = self._build_messages(
            campaign_state=payload.campaign_state,
            recent_turns=payload.recent_turns,
            memory_context=memory_context,
            message=payload.player_message,
            parsed_action=payload.parsed_action,
            tool_result=payload.tool_result,
        )
        reply = await model_client.generate_text(
            messages=messages,
            model=model or ModelPolicy.narrator_model(),
            max_output_tokens=TokenBudget.narrator_max_output_tokens(),
            reasoning_effort="medium",
            timeout=20,
        )
        return NarratorAgentOutput(reply_text=reply)

    def _build_messages(
        self,
        *,
        campaign_state: str,
        recent_turns: list[dict[str, str]],
        memory_context: list[dict[str, str]],
        message: str,
        parsed_action: ParsedAction | None,
        tool_result: ToolExecutionResult | None,
    ) -> list[ChatCompletionMessageParam]:
        messages: list[ChatCompletionMessageParam] = [
            {
                "role": "developer",
                "content": narrator_prompt,
            },
            {
                "role": "user",
                "content": f"Campaign state:\n{campaign_state}".strip(),
            },
        ]

        if memory_context:
            messages.append(
                {
                    "role": "user",
                    "content": "Relevant memory:\n" + "\n\n".join(
                        entry.get("content", "") for entry in memory_context if entry.get("content")
                    ),
                }
            )

        for turn in recent_turns[-8:]:
            role = turn.get("role", "")
            if role not in {"user", "assistant", "system"}:
                continue
            messages.append(
                cast(
                    ChatCompletionMessageParam,
                    {
                        "role": role,
                        "content": turn.get("content", ""),
                    },
                )
            )

        if parsed_action is not None:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Parsed player intent:\n"
                        f"action={parsed_action.action}, target={parsed_action.target}, "
                        f"stealth={parsed_action.stealth}, confidence={parsed_action.confidence:.2f}, "
                        f"status={parsed_action.parse_status}, notes={parsed_action.parser_notes}"
                    ),
                }
            )

        if tool_result is not None:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Tool execution result:\n"
                        "Authoritative structured payload follows.\n"
                        f"{tool_result.model_dump_json(exclude_none=True, indent=2)}"
                    ),
                }
            )

        messages.append(
            {
                "role": "user",
                "content": message,
            }
        )
        return messages
