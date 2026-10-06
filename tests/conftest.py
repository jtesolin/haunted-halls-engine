import os
import random
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config

from app.agents.starter_abilities import StarterAbilityGenerator
from app.core.config import settings
from app.ai.model_client import ModelCallResult, ModelUsage, model_client
from app.db.schema import metadata
from app.db.session import get_engine
from app.agents.director import DirectorProposalResponse
from app.schemas.chat import ActionParserOutput
from tests.factories import fake_action_parser_output, starter_ability_generation

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
def openai_test_configuration() -> Iterator[None]:
    original_key = settings.OPENAI_API_KEY
    settings.OPENAI_API_KEY = "test-only"
    try:
        yield
    finally:
        settings.OPENAI_API_KEY = original_key


@pytest.fixture(autouse=True)
def no_live_model_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail_unmocked_model_call(*args, **kwargs):
        raise AssertionError("unexpected live model call")

    monkeypatch.setattr(model_client, "generate_text", fail_unmocked_model_call)
    monkeypatch.setattr(model_client, "generate_structured", fail_unmocked_model_call)


@pytest.fixture
def fake_runtime_model_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_generate_text(*, messages, **kwargs):
        content = next(
            (
                message.get("content", "")
                for message in reversed(messages)
                if message.get("role") == "user"
            ),
            "",
        )
        if "campaign title" in str(content).lower():
            output = "The Bell Beneath the Hall"
        elif "opening scene" in str(content).lower():
            output = "You stand in the Entry Hall. What do you do first?"
        else:
            output = f"AI narrator replies: {content}"
        return ModelCallResult(
            output=output,
            usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
        )

    async def fake_generate_structured(*, messages, response_model, **kwargs):
        if response_model is ActionParserOutput:
            return fake_action_parser_output(messages)
        if response_model is DirectorProposalResponse:
            return response_model.model_validate({"proposal": {"decision": "none"}})
        raise AssertionError(
            f"Unexpected structured provider request in test: {response_model.__name__}"
        )

    monkeypatch.setattr(model_client, "generate_text", fake_generate_text)
    monkeypatch.setattr(model_client, "generate_structured", fake_generate_structured)


@pytest.fixture(autouse=True)
def no_live_starter_ability_provider(monkeypatch) -> None:
    async def fake_generate(
        self,
        return_usage: bool = False,
    ):
        generation = starter_ability_generation()
        if return_usage:
            return ModelCallResult(
                output=generation,
                usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
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