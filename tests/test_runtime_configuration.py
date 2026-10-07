import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.config import Settings, settings


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("MAX_DAILY_PLAYER_TOKENS", 300000),
        ("MAX_DAILY_PLAYER_REQUESTS", 100),
        ("MAX_DAILY_PROJECT_TOKENS", 1000000),
        ("MAX_DAILY_PROJECT_REQUESTS", 1000),
        ("MAX_TURNS_PER_CAMPAIGN", 20),
    ],
)
def test_usage_limit_defaults_and_environment_overrides(
    monkeypatch: pytest.MonkeyPatch, name: str, expected: int
) -> None:
    monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    assert getattr(Settings(OTEL_ENABLED=False), name) == expected

    monkeypatch.setenv(name, str(expected + 1))
    assert getattr(Settings(OTEL_ENABLED=False), name) == expected + 1


@pytest.mark.parametrize("api_key", [None, "", "   ", "\t\n"])
def test_application_startup_rejects_missing_or_whitespace_openai_key(
    monkeypatch: pytest.MonkeyPatch, api_key: str | None
) -> None:
    monkeypatch.setattr(settings, "OPENAI_API_KEY", api_key)

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY must be configured"):
        with TestClient(app):
            pytest.fail("Application started without a configured provider key.")


def test_application_startup_accepts_configured_key_without_provider_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "test-only")

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
