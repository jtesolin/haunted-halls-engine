from __future__ import annotations

import json
from datetime import datetime
from sqlite3 import Connection
from typing import Any, Optional

from app.db.models import CampaignDBModel, CharacterDBModel, GameEventDBModel, SummaryDBModel, TurnDBModel


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

    def save_turn(
        self,
        campaign_id: str,
        turn_id: str,
        player_message: str,
        ai_reply: str,
    ) -> TurnDBModel:
        created_at = datetime.utcnow().isoformat()
        self.conn.execute(
            "INSERT INTO turns (campaign_id, turn_id, player_message, ai_reply, created_at) VALUES (?, ?, ?, ?, ?)",
            (campaign_id, turn_id, player_message, ai_reply, created_at),
        )
        return TurnDBModel(campaign_id, turn_id, player_message, ai_reply, datetime.fromisoformat(created_at))

    def add_event(self, event_id: str, campaign_id: str, type: str, payload: Optional[dict[str, Any]] = None) -> GameEventDBModel:
        created_at = datetime.utcnow().isoformat()
        payload_json = json.dumps(payload) if payload is not None else None
        self.conn.execute(
            "INSERT INTO game_events (event_id, campaign_id, type, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            (event_id, campaign_id, type, payload_json, created_at),
        )
        return GameEventDBModel(event_id, campaign_id, type, payload_json, datetime.fromisoformat(created_at))

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
            "SELECT campaign_id, turn_id, player_message, ai_reply, created_at FROM turns WHERE campaign_id = ? ORDER BY created_at DESC LIMIT ?",
            (campaign_id, limit + 1),
        ).fetchall()
        turns = [
            TurnDBModel(
                campaign_id=row["campaign_id"],
                turn_id=row["turn_id"],
                player_message=row["player_message"],
                ai_reply=row["ai_reply"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows[:limit]
        ]
        turns.reverse()
        truncated = len(rows) > limit
        return campaign, turns, truncated

    def list_campaign_summaries_for_player(self, player_id: str) -> list[dict[str, Optional[str]]]:
        rows = self.conn.execute(
            """
            SELECT
                c.campaign_id AS campaign_id,
                c.name AS title,
                (SELECT t.ai_reply FROM turns t WHERE t.campaign_id = c.campaign_id ORDER BY created_at DESC LIMIT 1) AS last_message
            FROM campaigns c
            WHERE c.player_id = ?
            ORDER BY c.created_at DESC
            """,
            (player_id,),
        ).fetchall()
        return [
            {
                "campaign_id": row["campaign_id"],
                "title": row["title"],
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
