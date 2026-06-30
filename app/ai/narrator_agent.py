from __future__ import annotations

from typing import Any

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
        prompt = self._build_prompt(
            campaign_state=campaign_state,
            recent_turns=recent_turns,
            message=message,
        )
        return await model_client.generate_text(
            prompt,
            model=model or settings.MODEL_NAME or "gpt-3.5-turbo",
            max_output_tokens=settings.MAX_OUTPUT_TOKENS,
            temperature=0.7,
            timeout=20,
        )

    def _build_prompt(
        self,
        *,
        campaign_state: str,
        recent_turns: list[dict[str, str]],
        message: str,
    ) -> str:
        history_lines: list[str] = []
        for index, turn in enumerate(recent_turns[-8:], start=1):
            history_lines.append(
                f"{index}. Player: {turn.get('player_message', '')}\n   Narrator: {turn.get('ai_reply', '')}"
            )

        if history_lines:
            recent_history = "\n".join(history_lines)
        else:
            recent_history = "No prior turns yet."

        return (
            f"{narrator_prompt}\n\n"
            f"Campaign state:\n{campaign_state}\n\n"
            f"Recent conversation:\n{recent_history}\n\n"
            f"Player says: {message}"
        )
