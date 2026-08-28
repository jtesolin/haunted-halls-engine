import os
import random
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config

from app.core.config import settings

TEST_INTERNAL_ENGINE_SERVICE_TOKEN = (
    "test-internal-engine-service-token-0000000000000000000000000000000000"
)


@pytest.fixture(autouse=True)
def deterministic_random() -> Iterator[None]:
    random.seed(20260821)
    yield


@pytest.fixture(autouse=True)
def internal_engine_service_token() -> Iterator[None]:
    original_token = settings.INTERNAL_ENGINE_SERVICE_TOKEN
    settings.INTERNAL_ENGINE_SERVICE_TOKEN = TEST_INTERNAL_ENGINE_SERVICE_TOKEN
    try:
        yield
    finally:
        settings.INTERNAL_ENGINE_SERVICE_TOKEN = original_token


@pytest.fixture(autouse=True)
def isolated_database(tmp_path) -> Iterator[None]:
    original_database_url = settings.DATABASE_URL
    settings.DATABASE_URL = f"sqlite:///{tmp_path / 'test_engine.db'}"
    try:
        alembic_config = Config("alembic.ini")
        command.upgrade(alembic_config, "head")
        yield
    finally:
        settings.DATABASE_URL = original_database_url