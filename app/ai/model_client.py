from __future__ import annotations

from typing import Any

from app.core.config import settings

try:
    from openai import AsyncOpenAI
except ImportError:  # pragma: no cover - dependency may be absent in some environments
    AsyncOpenAI = None


class ModelClient:
    def __init__(self) -> None:
        self._client = None

    def _get_client(self):
        if self._client is None and AsyncOpenAI and settings.OPENAI_API_KEY:
            self._client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        return self._client

    async def generate_text(self, prompt: str, **kwargs: Any) -> str:
        client = self._get_client()
        if client is None:
            return self._fake_ai_narration(prompt)

        model = kwargs.get("model") or settings.MODEL_NAME or "gpt-4o-mini"
        max_output_tokens = kwargs.get("max_output_tokens", settings.MAX_OUTPUT_TOKENS)
        temperature = kwargs.get("temperature", 0.7)

        response = await self._client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_output_tokens,
            temperature=temperature,
        )
        content = response.choices[0].message.content if response.choices else ""
        return content or self._fake_ai_narration(prompt)

    async def generate_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        reply = await self.generate_text(prompt, **kwargs)
        return {"reply": reply}

    def _fake_ai_narration(self, prompt: str) -> str:
        return f"AI narrator replies: {prompt}"


model_client = ModelClient()
