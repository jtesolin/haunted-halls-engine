from __future__ import annotations

from typing import Any

from app.core.config import settings


class ModelClient:
    async def generate_text(self, prompt: str, **kwargs: Any) -> str:
        return self._fake_ai_narration(prompt)

    async def generate_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        # Future-proof interface for structured responses.
        return {"reply": self._fake_ai_narration(prompt)}

    def _fake_ai_narration(self, prompt: str) -> str:
        return f"AI narrator replies: {prompt}"


model_client = ModelClient()
