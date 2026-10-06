import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.config import settings


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
