from __future__ import annotations

from typing import Any, TYPE_CHECKING

from app.core.config import settings

if TYPE_CHECKING:
    from openai import AsyncOpenAI

try:
    from openai import AsyncOpenAI as OpenAIAsyncClient
except ImportError:  # pragma: no cover - dependency may be absent in some environments
    OpenAIAsyncClient = None


class ModelClient:
    def __init__(self) -> None:
        self._client: AsyncOpenAI | None = None

    def _get_client(self) -> AsyncOpenAI | None:
        if self._client is None and OpenAIAsyncClient and settings.OPENAI_API_KEY:
            self._client = OpenAIAsyncClient(api_key=settings.OPENAI_API_KEY)
        return self._client

    async def generate_text(self, prompt: str, **kwargs: Any) -> str:
        client = self._get_client()
        if client is None:
            return self._fake_ai_narration(prompt)
        assert client is not None

        model = kwargs.get("model") or settings.DEFAULT_MODEL_NAME or "gpt-4o-mini"
        max_output_tokens = kwargs.get("max_output_tokens", settings.MAX_OUTPUT_TOKENS)
        temperature = kwargs.get("temperature", 0.7)

        response = await client.chat.completions.create(
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
