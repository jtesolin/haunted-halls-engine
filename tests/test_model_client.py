import asyncio
from typing import Any

from app.ai.model_client import ModelClient


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
