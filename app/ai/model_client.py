from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeVar, cast, overload

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam
from openai.types.shared.reasoning_effort import ReasoningEffort
from openai.types.shared_params.reasoning import Reasoning
from pydantic import BaseModel

from app.core.config import settings


StructuredResponseT = TypeVar("StructuredResponseT", bound=BaseModel)
ModelOutputT = TypeVar("ModelOutputT")


@dataclass
class ModelUsage:
    input_tokens: int | None
    cached_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass
class ModelCallResult(Generic[ModelOutputT]):
    output: ModelOutputT | None
    usage: ModelUsage | None = None


class ModelClient:
    def __init__(self) -> None:
        self._client: AsyncOpenAI | None = None

    def _get_client(self) -> AsyncOpenAI | None:
        api_key = (settings.OPENAI_API_KEY or "").strip()
        if self._client is None and api_key:
            self._client = AsyncOpenAI(api_key=api_key)
        return self._client

    @overload
    async def generate_text(
        self,
        *,
        messages: list[ChatCompletionMessageParam],
        reasoning_effort: ReasoningEffort,
        model: str,
        max_output_tokens: int,
        timeout: int,
        retry_reasoning_effort: ReasoningEffort = "minimal",
        return_usage: Literal[False] = False,
    ) -> str: ...

    @overload
    async def generate_text(
        self,
        *,
        messages: list[ChatCompletionMessageParam],
        reasoning_effort: ReasoningEffort,
        model: str,
        max_output_tokens: int,
        timeout: int,
        retry_reasoning_effort: ReasoningEffort = "minimal",
        return_usage: Literal[True],
    ) -> ModelCallResult[str]: ...

    async def generate_text(
        self,
        *,
        messages: list[ChatCompletionMessageParam],
        reasoning_effort: ReasoningEffort,
        model: str,
        max_output_tokens: int,
        timeout: int,
        retry_reasoning_effort: ReasoningEffort = "minimal",
        return_usage: bool = False,
    ) -> str | ModelCallResult[str]:
        client = self._get_client()

        if client is None:
            content = self._fake_ai_narration(messages)
            return ModelCallResult(output=content, usage=None) if return_usage else content
        assert client is not None

        request_model = model
        request_max_output_tokens = max_output_tokens

        request_reasoning: Reasoning = {"effort": reasoning_effort}
        request_kwargs: dict[str, Any] = {
            "model": request_model,
            "input": cast(Any, self._to_responses_input(messages)),
            "max_output_tokens": request_max_output_tokens,
            "timeout": timeout,
            "reasoning": request_reasoning,
        }

        response = await client.responses.create(
            **request_kwargs,
        )
        content = self._extract_response_text(response)
        usage = self._extract_usage(response)

        if content:
            result = ModelCallResult(output=content, usage=usage)
            return result if return_usage else content

        if self._is_incomplete_max_tokens(response):
            retry_kwargs = dict(request_kwargs)
            retry_reasoning: Reasoning = {"effort": retry_reasoning_effort}
            retry_kwargs["reasoning"] = retry_reasoning
            response = await client.responses.create(
                **retry_kwargs,
            )
            content = self._extract_response_text(response)
            usage = self._extract_usage(response)

        content = content or self._fake_ai_narration(messages)
        result = ModelCallResult(output=content, usage=usage)
        return result if return_usage else content

    @overload
    async def generate_structured(
        self,
        *,
        messages: list[ChatCompletionMessageParam],
        response_model: type[StructuredResponseT],
        reasoning_effort: ReasoningEffort,
        model: str,
        max_output_tokens: int,
        timeout: int,
        return_usage: Literal[False] = False,
    ) -> StructuredResponseT | None: ...

    @overload
    async def generate_structured(
        self,
        *,
        messages: list[ChatCompletionMessageParam],
        response_model: type[StructuredResponseT],
        reasoning_effort: ReasoningEffort,
        model: str,
        max_output_tokens: int,
        timeout: int,
        return_usage: Literal[True],
    ) -> ModelCallResult[StructuredResponseT]: ...

    async def generate_structured(
        self,
        *,
        messages: list[ChatCompletionMessageParam],
        response_model: type[StructuredResponseT],
        reasoning_effort: ReasoningEffort,
        model: str,
        max_output_tokens: int,
        timeout: int,
        return_usage: bool = False,
    ) -> StructuredResponseT | ModelCallResult[StructuredResponseT] | None:
        client = self._get_client()
        if client is None:
            raise RuntimeError("Structured model output requires an OpenAI client.")

        request_reasoning: Reasoning = {"effort": reasoning_effort}
        response = await client.responses.parse(
            model=model,
            input=cast(Any, self._to_responses_input(messages)),
            max_output_tokens=max_output_tokens,
            timeout=timeout,
            reasoning=request_reasoning,
            text_format=response_model,
        )

        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            result = ModelCallResult(output=None, usage=self._extract_usage(response))
            return result if return_usage else None
        if isinstance(parsed, response_model):
            payload = parsed
        else:
            payload = response_model.model_validate(parsed)
        result = ModelCallResult(output=payload, usage=self._extract_usage(response))
        return result if return_usage else payload

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

    def _extract_usage(self, response: Any) -> ModelUsage | None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        
        input_tokens = getattr(usage, "input_tokens", None)
        output_tokens = getattr(usage, "output_tokens", None)
        total_tokens = getattr(usage, "total_tokens", None)
        
        # Extract detail objects if present
        input_details = getattr(usage, "input_tokens_details", None)
        cached_input = getattr(input_details, "cached_tokens", None) if input_details else None
        cache_write_input = getattr(input_details, "cache_write_tokens", None) if input_details else None
        
        output_details = getattr(usage, "output_tokens_details", None)
        reasoning_output = getattr(output_details, "reasoning_tokens", None) if output_details else None
        
        # Return None only if all fields are None
        if all(v is None for v in [input_tokens, output_tokens, total_tokens, cached_input, cache_write_input, reasoning_output]):
            return None
        
        return ModelUsage(
            input_tokens=int(input_tokens) if input_tokens is not None else None,
            cached_input_tokens=int(cached_input) if cached_input is not None else None,
            cache_write_input_tokens=int(cache_write_input) if cache_write_input is not None else None,
            output_tokens=int(output_tokens) if output_tokens is not None else None,
            reasoning_output_tokens=int(reasoning_output) if reasoning_output is not None else None,
            total_tokens=int(total_tokens) if total_tokens is not None else None,
        )

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
