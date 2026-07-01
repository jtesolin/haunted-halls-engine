from __future__ import annotations

import json
from datetime import datetime
from sqlite3 import Connection
from typing import Optional

from app.db.models import CampaignDBModel, CharacterDBModel, GameEventDBModel, SummaryDBModel, TurnDBModel
from app.schemas.events import GameEventPayload, GameEventType


class Repository:
    def __init__(self, conn: Connection) -> None:
        self.conn = conn

    def create_campaign(
        self,
        campaign_id: str,
        player_id: str,
        name: str,
        description: Optional[str] = None,
        state: Optional[dict[str, Any]] = None,
    ) -> CampaignDBModel:
        created_at = datetime.utcnow().isoformat()
        state_json = json.dumps(state) if state is not None else None
        self.conn.execute(
            "INSERT OR IGNORE INTO campaigns (campaign_id, player_id, name, description, state, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (campaign_id, player_id, name, description, state_json, created_at),
        )
        return CampaignDBModel(campaign_id, player_id, name, description, state_json, datetime.fromisoformat(created_at))

    def get_campaign(self, campaign_id: str) -> Optional[CampaignDBModel]:
        row = self.conn.execute(
            "SELECT campaign_id, player_id, name, description, state, created_at FROM campaigns WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchone()
        if row is None:
            return None
        return CampaignDBModel(
            campaign_id=row["campaign_id"],
            player_id=row["player_id"],
            name=row["name"],
            description=row["description"],
            state=row["state"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def get_player_campaign(self, player_id: str, campaign_id: str) -> Optional[CampaignDBModel]:
        row = self.conn.execute(
            "SELECT campaign_id, player_id, name, description, state, created_at FROM campaigns WHERE campaign_id = ? AND player_id = ?",
            (campaign_id, player_id),
        ).fetchone()
        if row is None:
            return None
        return CampaignDBModel(
            campaign_id=row["campaign_id"],
            player_id=row["player_id"],
            name=row["name"],
            description=row["description"],
            state=row["state"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def count_player_campaigns(self, player_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS total FROM campaigns WHERE player_id = ?",
            (player_id,),
        ).fetchone()
        return int(row["total"] or 0)

    def count_player_requests_since(self, player_id: str, since_iso: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS total FROM model_requests WHERE player_id = ? AND created_at >= ?",
            (player_id, since_iso),
        ).fetchone()
        return int(row["total"] or 0)

    def sum_player_estimated_input_tokens_since(self, player_id: str, since_iso: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(estimated_input_tokens), 0) AS total FROM model_requests WHERE player_id = ? AND created_at >= ?",
            (player_id, since_iso),
        ).fetchone()
        return int(row["total"] or 0)

    def count_campaign_turns(self, player_id: str, campaign_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS total FROM turns t JOIN campaigns c ON t.campaign_id = c.campaign_id WHERE t.campaign_id = ? AND c.player_id = ? AND t.role = 'user'",
            (campaign_id, player_id),
        ).fetchone()
        return int(row["total"] or 0)

    def log_model_request(
        self,
        request_id: str,
        player_id: str,
        campaign_id: str,
        turn_id: str,
        agent_name: str,
        model: str,
        estimated_input_tokens: int,
        actual_input_tokens: int,
        actual_output_tokens: int,
        latency_ms: int,
        success: bool,
        failure_reason: str | None = None,
        cost_estimate: float | None = None,
    ) -> None:
        created_at = datetime.utcnow().isoformat()
        self.conn.execute(
            "INSERT INTO model_requests (request_id, player_id, campaign_id, turn_id, agent_name, model, estimated_input_tokens, actual_input_tokens, actual_output_tokens, latency_ms, success, failure_reason, cost_estimate, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                request_id,
                player_id,
                campaign_id,
                turn_id,
                agent_name,
                model,
                estimated_input_tokens,
                actual_input_tokens,
                actual_output_tokens,
                latency_ms,
                int(success),
                failure_reason,
                cost_estimate,
                created_at,
            ),
        )

    def create_turn(
        self,
        player_id: str,
        campaign_id: str,
        turn_id: str,
        role: str,
        content: str,
    ) -> TurnDBModel:
        created_at = datetime.utcnow().isoformat()
        self.conn.execute(
            "INSERT INTO turns (turn_id, player_id, campaign_id, role, content, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (turn_id, player_id, campaign_id, role, content, created_at),
        )
        return TurnDBModel(turn_id, player_id, campaign_id, role, content, datetime.fromisoformat(created_at))

    def add_event(
        self,
        event_id: str,
        player_id: str,
        campaign_id: str,
        turn_id: str,
        type: GameEventType,
        payload: Optional[GameEventPayload] = None,
    ) -> GameEventDBModel:
        created_at = datetime.utcnow().isoformat()
        payload_json = payload.model_dump_json() if payload is not None else None
        self.conn.execute(
            "INSERT INTO game_events (event_id, player_id, campaign_id, turn_id, type, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event_id, player_id, campaign_id, turn_id, type, payload_json, created_at),
        )
        return GameEventDBModel(event_id, player_id, campaign_id, turn_id, type, payload_json, datetime.fromisoformat(created_at))

    def add_summary(self, campaign_id: str, summary: str) -> SummaryDBModel:
        created_at = datetime.utcnow().isoformat()
        self.conn.execute(
            "INSERT INTO summaries (campaign_id, summary, created_at) VALUES (?, ?, ?, ?)",
            (campaign_id, summary, created_at),
        )
        return SummaryDBModel(campaign_id, summary, datetime.fromisoformat(created_at))

    def get_campaign_with_turns(self, campaign_id: str, limit: int = 10) -> tuple[Optional[CampaignDBModel], list[TurnDBModel], bool]:
        campaign = self.get_campaign(campaign_id)
        if campaign is None:
            return None, [], False

        rows = self.conn.execute(
            "SELECT turn_id, player_id, campaign_id, role, content, created_at FROM turns WHERE campaign_id = ? ORDER BY created_at DESC LIMIT ?",
            (campaign_id, limit + 1),
        ).fetchall()
        turns = [
            TurnDBModel(
                turn_id=row["turn_id"],
                player_id=row["player_id"],
                campaign_id=row["campaign_id"],
                role=row["role"],
                content=row["content"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows[:limit]
        ]
        turns.reverse()
        truncated = len(rows) > limit
        return campaign, turns, truncated

    def list_campaign_events(self, campaign_id: str, limit: int = 100) -> list[GameEventDBModel]:
        rows = self.conn.execute(
            "SELECT event_id, player_id, campaign_id, turn_id, type, payload_json, created_at FROM game_events WHERE campaign_id = ? ORDER BY created_at ASC LIMIT ?",
            (campaign_id, limit),
        ).fetchall()
        return [
            GameEventDBModel(
                event_id=row["event_id"],
                player_id=row["player_id"],
                campaign_id=row["campaign_id"],
                turn_id=row["turn_id"],
                type=row["type"],
                payload_json=row["payload_json"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def list_campaign_summaries_for_player(self, player_id: str) -> list[dict[str, Optional[str]]]:
        rows = self.conn.execute(
            """
            SELECT
                c.campaign_id AS campaign_id,
                c.name AS name,
                (
                    SELECT t.content
                    FROM turns t
                    WHERE t.campaign_id = c.campaign_id AND t.role = 'assistant'
                    ORDER BY created_at DESC
                    LIMIT 1
                ) AS last_message
            FROM campaigns c
            WHERE c.player_id = ?
            ORDER BY c.created_at DESC
            """,
            (player_id,),
        ).fetchall()
        return [
            {
                "campaign_id": row["campaign_id"],
                "name": row["name"],
                "last_message": row["last_message"],
            }
            for row in rows
        ]

    def get_character(self, character_id: str) -> Optional[CharacterDBModel]:
        row = self.conn.execute(
            "SELECT character_id, player_id, campaign_id, name, description, created_at FROM characters WHERE character_id = ?",
            (character_id,),
        ).fetchone()
        if row is None:
            return None
        return CharacterDBModel(
            character_id=row["character_id"],
            player_id=row["player_id"],
            campaign_id=row["campaign_id"],
            name=row["name"],
            description=row["description"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def list_characters_for_player(self, player_id: str) -> list[CharacterDBModel]:
        rows = self.conn.execute(
            "SELECT character_id, player_id, campaign_id, name, description, created_at FROM characters WHERE player_id = ? ORDER BY created_at DESC",
            (player_id,),
        ).fetchall()
        return [
            CharacterDBModel(
                character_id=row["character_id"],
                player_id=row["player_id"],
                campaign_id=row["campaign_id"],
                name=row["name"],
                description=row["description"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]
