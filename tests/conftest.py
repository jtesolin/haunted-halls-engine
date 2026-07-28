from collections.abc import Iterator

import pytest

from app.core.config import settings

TEST_INTERNAL_ENGINE_SERVICE_TOKEN = (
    "test-internal-engine-service-token-0000000000000000000000000000000000"
)


@pytest.fixture(autouse=True)
def internal_engine_service_token() -> Iterator[None]:
    original_token = settings.INTERNAL_ENGINE_SERVICE_TOKEN
    settings.INTERNAL_ENGINE_SERVICE_TOKEN = TEST_INTERNAL_ENGINE_SERVICE_TOKEN
    try:
        yield
    finally:
        settings.INTERNAL_ENGINE_SERVICE_TOKEN = original_token