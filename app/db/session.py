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
    _ensure_sqlite_directory(url)
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False} if url.startswith("sqlite") else {},
    )

    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
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
def session() -> Iterator[Repository]:
    conn = get_connection()
    transaction = conn.begin()
    if conn.dialect.name == "sqlite":
        conn.exec_driver_sql("BEGIN")
    try:
        yield Repository(conn)
        transaction.commit()
    except Exception:
        transaction.rollback()
        raise
    finally:
        conn.close()
