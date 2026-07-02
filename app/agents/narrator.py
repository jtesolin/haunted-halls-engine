from __future__ import annotations

from openai.types.chat import ChatCompletionMessageParam

from app.ai.model_client import model_client
from app.ai.prompts import narrator_prompt
from app.core.config import settings
from app.schemas.chat import ParsedAction, ToolExecutionResult


class NarratorAgent2:
    async def generate(
        self,
        *,
        campaign_state: str,
        recent_turns: list[dict[str, str]],
        message: str,
        parsed_action: ParsedAction | None = None,
        tool_result: ToolExecutionResult | None = None,
        model: str | None = None,
    ) -> str:
        messages = self._build_messages(
            campaign_state=campaign_state,
            recent_turns=recent_turns,
            message=message,
            parsed_action=parsed_action,
            tool_result=tool_result,
        )
        return await model_client.generate_text(
            messages=messages,
            model=model or settings.DEFAULT_MODEL_NAME or "gpt-4o-mini",
            max_output_tokens=settings.MAX_OUTPUT_TOKENS,
            temperature=0.7,
            timeout=20,
        )

    def _build_messages(
        self,
        *,
        campaign_state: str,
        recent_turns: list[dict[str, str]],
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

        for turn in recent_turns[-8:]:
            role = turn.get("role", "")
            if role not in {"user", "assistant", "system"}:
                continue
            messages.append(
                {
                    "role": role,
                    "content": turn.get("content", ""),
                }
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
                        f"success={tool_result.success}, tools={tool_result.applied_tools}, "
                        f"summary={tool_result.summary}, delta={tool_result.state_delta}, errors={tool_result.errors}"
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