from __future__ import annotations

from typing import Any, cast

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
        reasoning_effort: str,
        model: str,
        max_output_tokens: int,
        timeout: int,
        retry_reasoning_effort: str = "minimal",
    ) -> str:
        client = self._get_client()

        if client is None:
            return self._fake_ai_narration(messages)
        assert client is not None

        request_model = model
        request_max_output_tokens = max_output_tokens

        request_kwargs: dict[str, Any] = {
            "model": request_model,
            "input": cast(Any, self._to_responses_input(messages)),
            "max_output_tokens": request_max_output_tokens,
            "timeout": timeout,
            "reasoning": {"effort": reasoning_effort},
        }

        response = await client.responses.create(
            **request_kwargs,
        )
        content = self._extract_response_text(response)

        if content:
            return content

        if self._is_incomplete_max_tokens(response):
            retry_kwargs = dict(request_kwargs)
            retry_kwargs["reasoning"] = {"effort": retry_reasoning_effort}
            response = await client.responses.create(
                **retry_kwargs,
            )
            content = self._extract_response_text(response)

        return content or self._fake_ai_narration(messages)

    def _is_incomplete_max_tokens(self, response: Any) -> bool:
        status = getattr(response, "status", None)
        if status != "incomplete":
            return False

        incomplete_details = getattr(response, "incomplete_details", None)
        reason = getattr(incomplete_details, "reason", None)
        return reason == "max_output_tokens"

    def _to_responses_input(self, messages: list[ChatCompletionMessageParam]) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        for turn in messages:
            role = turn.get("role")
            content = turn.get("content")
            if role is None:
                continue

            content_type = "output_text" if role == "assistant" else "input_text"

            if isinstance(content, str):
                converted.append(
                    {
                        "role": role,
                        "content": [
                            {
                                "type": content_type,
                                "text": content,
                            }
                        ],
                    }
                )
                continue

            if isinstance(content, list):
                converted.append({"role": role, "content": content})
                continue

            converted.append(
                {
                    "role": role,
                    "content": [
                        {
                            "type": content_type,
                            "text": str(content),
                        }
                    ],
                }
            )

        return converted

    def _extract_response_text(self, response: Any) -> str:
        output_text = getattr(response, "output_text", None)
        if isinstance(output_text, str) and output_text.strip():
            return output_text

        chunks: list[str] = []
        output = getattr(response, "output", [])
        for item in output or []:
            for content_item in getattr(item, "content", []) or []:
                text = getattr(content_item, "text", None)
                if isinstance(text, str) and text:
                    chunks.append(text)

        return "\n".join(chunks).strip()

    def _fake_ai_narration(self, messages: list[ChatCompletionMessageParam]) -> str:
        for turn in reversed(messages):
            if turn.get("role") == "user" and isinstance(turn.get("content"), str):
                return f"AI narrator replies: {turn.get('content', '')}"
        return "AI narrator replies:"


model_client = ModelClient()
