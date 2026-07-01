from __future__ import annotations

from openai.types.chat import ChatCompletionMessageParam

from app.ai.model_client import model_client
from app.ai.prompts import narrator_prompt
from app.core.config import settings


class NarratorAgent2:
    async def generate(
        self,
        *,
        campaign_state: str,
        recent_turns: list[dict[str, str]],
        message: str,
        model: str | None = None,
    ) -> str:
        messages = self._build_messages(
            campaign_state=campaign_state,
            recent_turns=recent_turns,
            message=message,
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

        messages.append(
            {
                "role": "user",
                "content": message,
            }
        )
        return messages
