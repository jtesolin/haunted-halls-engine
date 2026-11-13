from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Connection, Engine

from app.core.config import settings
from app.db.repositories import Repository

DEFAULT_SQLITE_PATH = Path("./data/haunted_halls.db")


def _database_url() -> str:
    if settings.DATABASE_URL:
        return settings.DATABASE_URL
    return f"sqlite:///{DEFAULT_SQLITE_PATH.resolve()}"


def _ensure_sqlite_directory(url: str) -> None:
    if not url.startswith("sqlite:///") or url in {"sqlite://", "sqlite:///:memory:"}:
        return
    database_path = Path(url.split("sqlite:///", 1)[1])
    database_path.parent.mkdir(parents=True, exist_ok=True)


def _create_engine() -> Engine:
    url = _database_url()
    engine_kwargs: dict[str, object] = {}
    if url.startswith("sqlite"):
        _ensure_sqlite_directory(url)
        engine_kwargs["connect_args"] = {"check_same_thread": False, "timeout": 15}
    else:
        engine_kwargs["pool_size"] = settings.DB_POOL_SIZE
        engine_kwargs["max_overflow"] = settings.DB_MAX_OVERFLOW

    engine = create_engine(url, **engine_kwargs)

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        @event.listens_for(engine, "begin")
        def _begin_sqlite_transaction(connection) -> None:
            connection.exec_driver_sql("BEGIN")

    return engine


_engine_url: str | None = None
_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine, _engine_url
    url = _database_url()
    if _engine is None or _engine_url != url:
        if _engine is not None:
            _engine.dispose()
        _engine = _create_engine()
        _engine_url = url
    return _engine


def get_connection() -> Connection:
    return get_engine().connect()


@contextmanager
def session(*, lock_budget: bool = False) -> Iterator[Repository]:
    """Short transaction. Never hold one across a model call.

    ``lock_budget`` takes the global usage-budget lock as the first statement
    (a write-first lock on SQLite, a row lock on PostgreSQL) so budget checks and
    their inserts are serialized.
    """
    conn = get_connection()
    transaction = conn.begin()
    try:
        repo = Repository(conn)
        if lock_budget:
            repo.lock_usage_budget()
        yield repo
        if transaction.is_active:
            transaction.commit()
    except Exception:
        if transaction.is_active:
            transaction.rollback()
        raise
    finally:
        conn.close()
