import os
import random
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config

from app.agents.starter_abilities import StarterAbilityGenerator
from app.core.config import settings
from app.ai.model_client import ModelCallResult, ModelUsage
from app.db.schema import metadata
from app.db.session import get_engine

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
def no_live_starter_ability_provider(monkeypatch) -> None:
    async def fake_generate(
        self,
        *,
        provider_model_enabled: bool,
        return_usage: bool = False,
    ):
        generation = self._stub_generation()
        if return_usage:
            return ModelCallResult(
                output=generation,
                usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2)
                if provider_model_enabled
                else None,
            )
        return generation

    monkeypatch.setattr(StarterAbilityGenerator, "generate", fake_generate)


@pytest.fixture(autouse=True)
def isolated_database(tmp_path) -> Iterator[None]:
    original_database_url = settings.DATABASE_URL
    test_database_url = os.environ.get("TEST_DATABASE_URL")
    settings.DATABASE_URL = test_database_url or f"sqlite:///{tmp_path / 'test_engine.db'}"
    try:
        if test_database_url:
            # Reset the shared test database so each test starts from a clean schema,
            # matching the isolation the per-test SQLite file otherwise provides.
            engine = get_engine()
            metadata.drop_all(engine)
            with engine.begin() as connection:
                connection.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
        alembic_config = Config("alembic.ini")
        command.upgrade(alembic_config, "head")
        yield
    finally:
        settings.DATABASE_URL = original_database_url