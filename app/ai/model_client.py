from __future__ import annotations

from typing import Any

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from app.core.config import settings


class ModelClient:
    def __init__(self) -> None:
        self._client: AsyncOpenAI | None = None

    def _get_client(self) -> AsyncOpenAI | None:
        if self._client is None and settings.OPENAI_API_KEY:
            self._client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        return self._client

    async def generate_text(
        self,
        *,
        messages: list[ChatCompletionMessageParam],
        **kwargs: Any,
    ) -> str:
        client = self._get_client()

        if client is None:
            return self._fake_ai_narration(messages)
        assert client is not None

        model = kwargs.get("model") or settings.DEFAULT_MODEL_NAME or "gpt-4o-mini"
        max_output_tokens = kwargs.get("max_output_tokens", settings.MAX_OUTPUT_TOKENS)
        temperature = kwargs.get("temperature", 0.7)

        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_output_tokens,
            temperature=temperature,
        )
        content = response.choices[0].message.content if response.choices else ""
        return content or self._fake_ai_narration(messages)

    def _fake_ai_narration(self, messages: list[ChatCompletionMessageParam]) -> str:
        for turn in reversed(messages):
            if turn.get("role") == "user" and isinstance(turn.get("content"), str):
                return f"AI narrator replies: {turn.get('content', '')}"
        return "AI narrator replies:"


model_client = ModelClient()
