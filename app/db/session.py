from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.core.config import settings
from app.db.repositories import Repository

DEFAULT_SQLITE_PATH = Path("./data/haunted_halls.db")


def _database_url() -> str:
    if settings.DATABASE_URL:
        return settings.DATABASE_URL
    return f"sqlite:///{DEFAULT_SQLITE_PATH.resolve()}"


def _sqlite_path_from_url(url: str) -> Path:
    if url.startswith("sqlite:///"):
        return Path(url.removeprefix("sqlite:///"))
    raise ValueError("Only sqlite:/// URLs are supported by the local session implementation.")


def get_connection() -> sqlite3.Connection:
    url = _database_url()
    db_path = _sqlite_path_from_url(url)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


@contextmanager
def session() -> Iterator[Repository]:
    conn = get_connection()
    try:
        init_db(conn)
        yield Repository(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS internal_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL UNIQUE,
            identity_provider TEXT NOT NULL,
            provider_issuer TEXT NOT NULL,
            provider_subject TEXT NOT NULL,
            email TEXT NOT NULL,
            email_verified INTEGER NOT NULL,
            display_name TEXT,
            avatar_url TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_login_at TEXT NOT NULL,
            UNIQUE(provider_issuer, provider_subject)
        );

        CREATE TABLE IF NOT EXISTS campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id TEXT NOT NULL UNIQUE,
            owner_user_id TEXT,
            name TEXT NOT NULL,
            description TEXT,
            state TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(owner_user_id) REFERENCES internal_users(user_id) ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS characters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            character_id TEXT NOT NULL UNIQUE,
            campaign_id TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(campaign_id) REFERENCES campaigns(campaign_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            turn_id TEXT NOT NULL UNIQUE,
            campaign_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(campaign_id) REFERENCES campaigns(campaign_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS model_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL UNIQUE,
            owner_user_id TEXT,
            campaign_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            model TEXT NOT NULL,
            estimated_input_tokens INTEGER NOT NULL,
            actual_input_tokens INTEGER NOT NULL,
            actual_output_tokens INTEGER NOT NULL,
            latency_ms INTEGER NOT NULL,
            success INTEGER NOT NULL,
            failure_reason TEXT,
            cost_estimate REAL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(owner_user_id) REFERENCES internal_users(user_id) ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS game_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            campaign_id TEXT NOT NULL,
            turn_id TEXT,
            type TEXT NOT NULL,
            payload_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(campaign_id) REFERENCES campaigns(campaign_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id TEXT NOT NULL,
            summary TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(campaign_id) REFERENCES campaigns(campaign_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id TEXT NOT NULL UNIQUE,
            campaign_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            content TEXT NOT NULL,
            embedding_json TEXT NOT NULL,
            importance REAL NOT NULL,
            source_event_id TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(campaign_id) REFERENCES campaigns(campaign_id) ON DELETE CASCADE
        );
        """
    )
