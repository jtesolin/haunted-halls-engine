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


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if not _column_exists(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate_turns_table(conn: sqlite3.Connection) -> None:
    columns = _table_columns(conn, "turns")
    if {"player_id", "role", "content"}.issubset(columns) and "player_message" not in columns and "ai_reply" not in columns:
        return

    conn.execute("ALTER TABLE turns RENAME TO turns_legacy")
    conn.execute(
        """
        CREATE TABLE turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            turn_id TEXT NOT NULL UNIQUE,
            player_id TEXT NOT NULL,
            campaign_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(campaign_id) REFERENCES campaigns(campaign_id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        INSERT INTO turns (turn_id, player_id, campaign_id, role, content, created_at)
        SELECT 'user_' || t.turn_id, COALESCE(c.player_id, ''), t.campaign_id, 'user', t.player_message, t.created_at
        FROM turns_legacy t
        JOIN campaigns c ON c.campaign_id = t.campaign_id
        """
    )
    conn.execute(
        """
        INSERT INTO turns (turn_id, player_id, campaign_id, role, content, created_at)
        SELECT t.turn_id, COALESCE(c.player_id, ''), t.campaign_id, 'assistant', t.ai_reply, t.created_at
        FROM turns_legacy t
        JOIN campaigns c ON c.campaign_id = t.campaign_id
        """
    )
    conn.execute("DROP TABLE turns_legacy")


def _migrate_game_events_table(conn: sqlite3.Connection) -> None:
    columns = _table_columns(conn, "game_events")
    if {"player_id", "turn_id", "payload_json"}.issubset(columns) and "payload" not in columns:
        return

    conn.execute("ALTER TABLE game_events RENAME TO game_events_legacy")
    conn.execute(
        """
        CREATE TABLE game_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            player_id TEXT NOT NULL,
            campaign_id TEXT NOT NULL,
            turn_id TEXT,
            type TEXT NOT NULL,
            payload_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(campaign_id) REFERENCES campaigns(campaign_id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        INSERT INTO game_events (event_id, player_id, campaign_id, turn_id, type, payload_json, created_at)
        SELECT ge.event_id, COALESCE(c.player_id, ''), ge.campaign_id, NULL, ge.type, ge.payload, ge.created_at
        FROM game_events_legacy ge
        JOIN campaigns c ON c.campaign_id = ge.campaign_id
        """
    )
    conn.execute("DROP TABLE game_events_legacy")


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
        CREATE TABLE IF NOT EXISTS campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id TEXT NOT NULL UNIQUE,
            player_id TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            state TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS characters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            character_id TEXT NOT NULL UNIQUE,
            player_id TEXT NOT NULL,
            campaign_id TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(campaign_id) REFERENCES campaigns(campaign_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            turn_id TEXT NOT NULL UNIQUE,
            player_id TEXT NOT NULL,
            campaign_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(campaign_id) REFERENCES campaigns(campaign_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS model_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL UNIQUE,
            player_id TEXT NOT NULL,
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
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS game_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            player_id TEXT NOT NULL,
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
        """
    )
    _migrate_turns_table(conn)
    _migrate_game_events_table(conn)
    _ensure_column(conn, "campaigns", "player_id", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "characters", "player_id", "TEXT NOT NULL DEFAULT ''")
