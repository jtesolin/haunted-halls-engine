from __future__ import annotations

from alembic import command
from alembic.config import Config
from sqlalchemy import Column, Integer, Table, inspect, text
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.db.session import get_engine, session
from app.db.repositories import Repository
from app.db.schema import metadata
from tests.db_helpers import migrate_database


EXPECTED_TABLES = {
    "alembic_version",
    "internal_users",
    "campaigns",
    "characters",
    "turns",
    "model_requests",
    "game_events",
    "summaries",
    "memories",
}


def test_fresh_database_has_full_alembic_schema() -> None:
    migrate_database()
    with get_engine().connect() as connection:
        assert set(inspect(connection).get_table_names()) == EXPECTED_TABLES
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert revision == "0001_initial_schema"


def test_session_commits_and_rolls_back() -> None:
    migrate_database()
    with session() as repository:
        repository.resolve_internal_user(
            identity_provider="google",
            provider_issuer="https://accounts.google.com",
            provider_subject="commit-user",
            email="commit@example.com",
            email_verified=True,
            display_name=None,
            avatar_url=None,
        )
    with session() as repository:
        assert repository.get_internal_user_by_id("missing") is None

    try:
        with session() as repository:
            repository.resolve_internal_user(
                identity_provider="google",
                provider_issuer="https://accounts.google.com",
                provider_subject="rollback-user",
                email="rollback@example.com",
                email_verified=True,
                display_name=None,
                avatar_url=None,
            )
            raise ValueError("force rollback")
    except ValueError:
        pass
    with session() as repository:
        assert repository.get_internal_user_by_identity(
            "https://accounts.google.com", "rollback-user"
        ) is None


def test_sqlite_foreign_keys_are_enforced() -> None:
    migrate_database()
    with get_engine().begin() as connection:
        try:
            connection.execute(
                text("INSERT INTO characters (character_id, campaign_id, name, created_at) VALUES (:id, :campaign, :name, :created)"),
                {"id": "character-orphan", "campaign": "missing", "name": "Orphan", "created": "2026-01-01T00:00:00"},
            )
        except IntegrityError:
            pass
        else:
            raise AssertionError("SQLite foreign keys are disabled")


def test_repository_uses_native_sqlalchemy_core() -> None:
    with get_engine().connect() as connection:
        repository = Repository(connection)
        row = repository.conn.execute(
            text("SELECT :value AS answer"),
            {"value": 42},
        ).mappings().fetchone()
        assert row is not None
        assert row["answer"] == 42


def test_initial_migration_does_not_follow_live_metadata(tmp_path) -> None:
    future_table = Table("future_table", metadata, Column("id", Integer, primary_key=True))
    original_database_url = settings.DATABASE_URL
    settings.DATABASE_URL = f"sqlite:///{tmp_path / 'future-schema.db'}"
    try:
        command.upgrade(Config("alembic.ini"), "head")
        with get_engine().connect() as connection:
            assert "future_table" not in inspect(connection).get_table_names()
    finally:
        metadata.remove(future_table)
        settings.DATABASE_URL = original_database_url


def test_database_url_fixture_is_isolated() -> None:
    assert settings.DATABASE_URL is not None