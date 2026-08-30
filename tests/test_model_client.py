import asyncio
from typing import Any

from types import SimpleNamespace

from pydantic import BaseModel

from app.ai.model_client import ModelClient, ModelUsage


class _FakeResponsesAPI:
    def __init__(self, response: Any) -> None:
        if isinstance(response, list):
            self._responses = list(response)
        else:
            self._responses = [response]
        self.last_kwargs: dict[str, Any] | None = None
        self.call_count = 0

    async def create(self, **kwargs: Any) -> Any:
        self.last_kwargs = kwargs
        self.call_count += 1
        index = min(self.call_count - 1, len(self._responses) - 1)
        return self._responses[index]

    async def parse(self, **kwargs: Any) -> Any:
        self.last_kwargs = kwargs
        self.call_count += 1
        index = min(self.call_count - 1, len(self._responses) - 1)
        return self._responses[index]


class _FakeClient:
    def __init__(self, response: Any) -> None:
        self.responses = _FakeResponsesAPI(response)


class _ResponseWithText:
    def __init__(self, text: str) -> None:
        self.output_text = text
        self.output: list[Any] = []


class _ContentItem:
    def __init__(self, text: str) -> None:
        self.text = text


class _OutputItem:
    def __init__(self, content: list[_ContentItem]) -> None:
        self.content = content


class _ResponseWithOutput:
    def __init__(self, output: list[_OutputItem]) -> None:
        self.output_text = ""
        self.output = output


class _IncompleteDetails:
    def __init__(self, reason: str) -> None:
        self.reason = reason


class _IncompleteResponseNoText:
    def __init__(self) -> None:
        self.output_text = ""
        self.output: list[Any] = []
        self.status = "incomplete"
        self.incomplete_details = _IncompleteDetails("max_output_tokens")


class _ActionPayload(BaseModel):
    action: str


class _ParsedResponse:
    def __init__(self, output_parsed: Any) -> None:
        self.output_parsed = output_parsed


def test_generate_text_uses_responses_create_and_maps_options(monkeypatch) -> None:
    model_client = ModelClient()
    fake_client = _FakeClient(_ResponseWithText("Model reply"))
    monkeypatch.setattr(model_client, "_get_client", lambda: fake_client)

    result = asyncio.run(
        model_client.generate_text(
            messages=[{"role": "user", "content": "hello"}],
            reasoning_effort="minimal",
            model="gpt-test",
            max_output_tokens=123,
            timeout=9,
        )
    )

    assert result == "Model reply"
    assert fake_client.responses.last_kwargs is not None
    assert fake_client.responses.last_kwargs["model"] == "gpt-test"
    assert fake_client.responses.last_kwargs["max_output_tokens"] == 123
    assert "temperature" not in fake_client.responses.last_kwargs
    assert fake_client.responses.last_kwargs["timeout"] == 9
    assert fake_client.responses.last_kwargs["input"] == [
        {
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
        }
    ]


def test_generate_text_retries_on_incomplete_with_no_text(monkeypatch) -> None:
    model_client = ModelClient()
    fake_client = _FakeClient([_IncompleteResponseNoText(), _ResponseWithText("Recovered output")])
    monkeypatch.setattr(model_client, "_get_client", lambda: fake_client)

    result = asyncio.run(
        model_client.generate_text(
            messages=[{"role": "user", "content": "hello"}],
            reasoning_effort="minimal",
            model="gpt-test",
            max_output_tokens=220,
            timeout=9,
        )
    )

    assert result == "Recovered output"
    assert fake_client.responses.call_count == 2
    assert fake_client.responses.last_kwargs is not None
    assert fake_client.responses.last_kwargs["reasoning"] == {"effort": "minimal"}


def test_generate_text_maps_assistant_turns_as_output_text(monkeypatch) -> None:
    model_client = ModelClient()
    fake_client = _FakeClient(_ResponseWithText("Model reply"))
    monkeypatch.setattr(model_client, "_get_client", lambda: fake_client)

    result = asyncio.run(
        model_client.generate_text(
            messages=[
                {"role": "user", "content": "Player enters the hall."},
                {"role": "assistant", "content": "You hear distant chains."},
                {"role": "user", "content": "I light a torch."},
            ],
            reasoning_effort="minimal",
            model="gpt-test",
            max_output_tokens=220,
            timeout=9,
        )
    )

    assert result == "Model reply"
    assert fake_client.responses.last_kwargs is not None
    assert fake_client.responses.last_kwargs["input"] == [
        {
            "role": "user",
            "content": [{"type": "input_text", "text": "Player enters the hall."}],
        },
        {
            "role": "assistant",
            "content": [{"type": "output_text", "text": "You hear distant chains."}],
        },
        {
            "role": "user",
            "content": [{"type": "input_text", "text": "I light a torch."}],
        },
    ]


def test_generate_text_extracts_text_from_output_chunks(monkeypatch) -> None:
    model_client = ModelClient()
    fake_client = _FakeClient(
        _ResponseWithOutput(
            [
                _OutputItem([_ContentItem("First line"), _ContentItem("Second line")]),
            ]
        )
    )
    monkeypatch.setattr(model_client, "_get_client", lambda: fake_client)

    result = asyncio.run(
        model_client.generate_text(
            messages=[{"role": "user", "content": "hello"}],
            reasoning_effort="minimal",
            model="gpt-test",
            max_output_tokens=220,
            timeout=9,
        )
    )

    assert result == "First line\nSecond line"


def test_generate_text_uses_fallback_when_client_unavailable(monkeypatch) -> None:
    model_client = ModelClient()
    monkeypatch.setattr(model_client, "_get_client", lambda: None)

    result = asyncio.run(
        model_client.generate_text(
            messages=[{"role": "user", "content": "hello"}],
            reasoning_effort="minimal",
            model="gpt-test",
            max_output_tokens=220,
            timeout=9,
        )
    )

    assert result == "AI narrator replies: hello"


def test_extract_usage_preserves_partial_and_zero_values() -> None:
    model_client = ModelClient()

    assert model_client._extract_usage(SimpleNamespace(usage=SimpleNamespace(input_tokens=100, output_tokens=50))) == ModelUsage(input_tokens=100, output_tokens=50)
    assert model_client._extract_usage(SimpleNamespace(usage=SimpleNamespace(input_tokens=100, output_tokens=None))) == ModelUsage(input_tokens=100, output_tokens=None)
    assert model_client._extract_usage(SimpleNamespace(usage=SimpleNamespace(input_tokens=None, output_tokens=50))) == ModelUsage(input_tokens=None, output_tokens=50)
    assert model_client._extract_usage(SimpleNamespace(usage=SimpleNamespace(input_tokens=None, output_tokens=None))) is None
    assert model_client._extract_usage(SimpleNamespace(usage=SimpleNamespace(input_tokens=0, output_tokens=0))) == ModelUsage(input_tokens=0, output_tokens=0)


def test_generate_structured_uses_responses_parse(monkeypatch) -> None:
    model_client = ModelClient()
    fake_client = _FakeClient(_ParsedResponse(_ActionPayload(action="move")))
    monkeypatch.setattr(model_client, "_get_client", lambda: fake_client)

    result = asyncio.run(
        model_client.generate_structured(
            messages=[{"role": "user", "content": "hello"}],
            response_model=_ActionPayload,
            reasoning_effort="minimal",
            model="gpt-test",
            max_output_tokens=64,
            timeout=7,
        )
    )

    assert result == _ActionPayload(action="move")
    assert fake_client.responses.last_kwargs is not None
    assert fake_client.responses.last_kwargs["model"] == "gpt-test"
    assert fake_client.responses.last_kwargs["text_format"] == _ActionPayload


def test_generate_structured_returns_none_when_output_not_parsed(monkeypatch) -> None:
    model_client = ModelClient()
    fake_client = _FakeClient(_ParsedResponse(None))
    monkeypatch.setattr(model_client, "_get_client", lambda: fake_client)

    result = asyncio.run(
        model_client.generate_structured(
            messages=[{"role": "user", "content": "hello"}],
            response_model=_ActionPayload,
            reasoning_effort="minimal",
            model="gpt-test",
            max_output_tokens=64,
            timeout=7,
        )
    )

    assert result is None
