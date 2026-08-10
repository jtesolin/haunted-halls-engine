from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from datetime import timezone
from sqlite3 import Connection
from typing import Any, Optional
from uuid import uuid4

from app.db.models import (
    CampaignDBModel,
    CharacterDBModel,
    GameEventDBModel,
    InternalUserDBModel,
    MemoryDBModel,
    SummaryDBModel,
    TurnDBModel,
)
from app.memory.retriever import build_embedding, cosine_similarity, deserialize_embedding, serialize_embedding
from app.schemas.events import GameEventPayload, GameEventType


class Repository:
    def __init__(self, conn: Connection) -> None:
        self.conn = conn

    def _now_utc_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _row_to_internal_user(self, row) -> InternalUserDBModel:
        return InternalUserDBModel(
            id=row["user_id"],
            identity_provider=row["identity_provider"],
            provider_issuer=row["provider_issuer"],
            provider_subject=row["provider_subject"],
            email=row["email"],
            email_verified=bool(row["email_verified"]),
            display_name=row["display_name"],
            avatar_url=row["avatar_url"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            last_login_at=datetime.fromisoformat(row["last_login_at"]),
        )

    def get_internal_user_by_identity(self, provider_issuer: str, provider_subject: str) -> InternalUserDBModel | None:
        row = self.conn.execute(
            """
            SELECT user_id, identity_provider, provider_issuer, provider_subject, email, email_verified,
                   display_name, avatar_url, created_at, updated_at, last_login_at
            FROM internal_users
            WHERE provider_issuer = ? AND provider_subject = ?
            """,
            (provider_issuer, provider_subject),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_internal_user(row)

    def get_internal_user_by_id(self, user_id: str) -> InternalUserDBModel | None:
        row = self.conn.execute(
            """
            SELECT user_id, identity_provider, provider_issuer, provider_subject, email, email_verified,
                   display_name, avatar_url, created_at, updated_at, last_login_at
            FROM internal_users
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_internal_user(row)

    def _insert_internal_user(
        self,
        *,
        user_id: str,
        identity_provider: str,
        provider_issuer: str,
        provider_subject: str,
        email: str,
        email_verified: bool,
        display_name: str | None,
        avatar_url: str | None,
        created_at: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO internal_users (
                user_id, identity_provider, provider_issuer, provider_subject,
                email, email_verified, display_name, avatar_url,
                created_at, updated_at, last_login_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                identity_provider,
                provider_issuer,
                provider_subject,
                email,
                int(email_verified),
                display_name,
                avatar_url,
                created_at,
                created_at,
                created_at,
            ),
        )

    def _update_internal_user_profile_and_login(
        self,
        *,
        user_id: str,
        email: str,
        email_verified: bool,
        display_name: str | None,
        avatar_url: str | None,
        now_iso: str,
    ) -> None:
        self.conn.execute(
            """
            UPDATE internal_users
            SET email = ?,
                email_verified = ?,
                display_name = ?,
                avatar_url = ?,
                updated_at = ?,
                last_login_at = ?
            WHERE user_id = ?
            """,
            (email, int(email_verified), display_name, avatar_url, now_iso, now_iso, user_id),
        )

    def resolve_internal_user(
        self,
        *,
        identity_provider: str,
        provider_issuer: str,
        provider_subject: str,
        email: str,
        email_verified: bool,
        display_name: str | None,
        avatar_url: str | None,
    ) -> InternalUserDBModel:
        existing = self.get_internal_user_by_identity(provider_issuer, provider_subject)
        now_iso = self._now_utc_iso()

        if existing is not None:
            self._update_internal_user_profile_and_login(
                user_id=existing.id,
                email=email,
                email_verified=email_verified,
                display_name=display_name,
                avatar_url=avatar_url,
                now_iso=now_iso,
            )
            refreshed = self.get_internal_user_by_identity(provider_issuer, provider_subject)
            if refreshed is None:
                raise RuntimeError("internal user disappeared during update")
            return refreshed

        user_id = f"user_{uuid4().hex}"
        try:
            self._insert_internal_user(
                user_id=user_id,
                identity_provider=identity_provider,
                provider_issuer=provider_issuer,
                provider_subject=provider_subject,
                email=email,
                email_verified=email_verified,
                display_name=display_name,
                avatar_url=avatar_url,
                created_at=now_iso,
            )
        except sqlite3.IntegrityError:
            pass

        resolved = self.get_internal_user_by_identity(provider_issuer, provider_subject)
        if resolved is None:
            raise RuntimeError("failed to resolve internal user")

        self._update_internal_user_profile_and_login(
            user_id=resolved.id,
            email=email,
            email_verified=email_verified,
            display_name=display_name,
            avatar_url=avatar_url,
            now_iso=self._now_utc_iso(),
        )
        refreshed = self.get_internal_user_by_identity(provider_issuer, provider_subject)
        if refreshed is None:
            raise RuntimeError("failed to reload resolved internal user")
        return refreshed

    def create_campaign(
        self,
        campaign_id: str,
        player_id: str,
        owner_user_id: str,
        name: str,
        description: Optional[str] = None,
        state: Optional[dict[str, Any]] = None,
    ) -> CampaignDBModel:
        created_at = datetime.utcnow().isoformat()
        state_json = json.dumps(state) if state is not None else None
        self.conn.execute(
            "INSERT OR IGNORE INTO campaigns (campaign_id, player_id, owner_user_id, name, description, state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (campaign_id, player_id, owner_user_id, name, description, state_json, created_at),
        )
        return CampaignDBModel(
            campaign_id,
            player_id,
            name,
            description,
            state_json,
            datetime.fromisoformat(created_at),
            owner_user_id=owner_user_id,
        )

    def _row_to_campaign(self, row) -> CampaignDBModel:
        return CampaignDBModel(
            campaign_id=row["campaign_id"],
            player_id=row["player_id"],
            name=row["name"],
            description=row["description"],
            state=row["state"],
            created_at=datetime.fromisoformat(row["created_at"]),
            owner_user_id=row["owner_user_id"],
        )

    def get_campaign(self, campaign_id: str) -> Optional[CampaignDBModel]:
        row = self.conn.execute(
            "SELECT campaign_id, player_id, owner_user_id, name, description, state, created_at FROM campaigns WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_campaign(row)

    def update_campaign_state(self, campaign_id: str, state: dict[str, Any]) -> None:
        self.conn.execute(
            "UPDATE campaigns SET state = ? WHERE campaign_id = ?",
            (json.dumps(state), campaign_id),
        )

    def get_player_campaign(self, player_id: str, campaign_id: str) -> Optional[CampaignDBModel]:
        row = self.conn.execute(
            "SELECT campaign_id, player_id, owner_user_id, name, description, state, created_at FROM campaigns WHERE campaign_id = ? AND player_id = ?",
            (campaign_id, player_id),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_campaign(row)

    def delete_player_campaign(self, player_id: str, campaign_id: str) -> bool:
        cursor = self.conn.execute(
            "DELETE FROM campaigns WHERE campaign_id = ? AND player_id = ?",
            (campaign_id, player_id),
        )
        return cursor.rowcount > 0

    def count_player_campaigns(self, player_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS total FROM campaigns WHERE player_id = ?",
            (player_id,),
        ).fetchone()
        return int(row["total"] or 0)

    def count_owner_campaigns(self, owner_user_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS total FROM campaigns WHERE owner_user_id = ?",
            (owner_user_id,),
        ).fetchone()
        return int(row["total"] or 0)

    def list_campaigns_for_owner(self, owner_user_id: str) -> list[dict[str, Optional[str]]]:
        rows = self.conn.execute(
            """
            SELECT
                c.campaign_id AS campaign_id,
                c.name AS name,
                (
                    SELECT t.content
                    FROM turns t
                    WHERE t.campaign_id = c.campaign_id AND t.role = 'assistant'
                    ORDER BY t.created_at DESC
                    LIMIT 1
                ) AS last_message
            FROM campaigns c
            WHERE c.owner_user_id = ?
            ORDER BY c.created_at DESC
            """,
            (owner_user_id,),
        ).fetchall()
        return [
            {
                "campaign_id": row["campaign_id"],
                "name": row["name"],
                "last_message": row["last_message"],
            }
            for row in rows
        ]

    def get_campaign_for_owner(self, campaign_id: str, owner_user_id: str) -> Optional[CampaignDBModel]:
        row = self.conn.execute(
            "SELECT campaign_id, player_id, owner_user_id, name, description, state, created_at FROM campaigns WHERE campaign_id = ? AND owner_user_id = ?",
            (campaign_id, owner_user_id),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_campaign(row)

    def delete_campaign_for_owner(self, campaign_id: str, owner_user_id: str) -> bool:
        cursor = self.conn.execute(
            "DELETE FROM campaigns WHERE campaign_id = ? AND owner_user_id = ?",
            (campaign_id, owner_user_id),
        )
        return cursor.rowcount > 0

    def get_campaign_with_turns_for_owner(
        self, campaign_id: str, owner_user_id: str, limit: int = 10
    ) -> tuple[Optional[CampaignDBModel], list[TurnDBModel], bool]:
        campaign = self.get_campaign_for_owner(campaign_id, owner_user_id)
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

    def get_character_for_owner(self, character_id: str, owner_user_id: str) -> Optional[CharacterDBModel]:
        row = self.conn.execute(
            """
            SELECT ch.character_id, ch.player_id, ch.campaign_id, ch.name, ch.description, ch.created_at
            FROM characters ch
            JOIN campaigns c ON ch.campaign_id = c.campaign_id
            WHERE ch.character_id = ? AND c.owner_user_id = ?
            """,
            (character_id, owner_user_id),
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

    def list_characters_for_owner(self, owner_user_id: str) -> list[CharacterDBModel]:
        rows = self.conn.execute(
            """
            SELECT ch.character_id, ch.player_id, ch.campaign_id, ch.name, ch.description, ch.created_at
            FROM characters ch
            JOIN campaigns c ON ch.campaign_id = c.campaign_id
            WHERE c.owner_user_id = ?
            ORDER BY ch.created_at DESC
            """,
            (owner_user_id,),
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
            "INSERT INTO summaries (campaign_id, summary, created_at) VALUES (?, ?, ?)",
            (campaign_id, summary, created_at),
        )
        return SummaryDBModel(campaign_id, summary, datetime.fromisoformat(created_at))

    def get_latest_summary(self, campaign_id: str) -> SummaryDBModel | None:
        row = self.conn.execute(
            "SELECT campaign_id, summary, created_at FROM summaries WHERE campaign_id = ? ORDER BY created_at DESC LIMIT 1",
            (campaign_id,),
        ).fetchone()
        if row is None:
            return None
        return SummaryDBModel(
            campaign_id=row["campaign_id"],
            summary=row["summary"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def list_campaign_summaries(self, campaign_id: str, limit: int = 5) -> list[SummaryDBModel]:
        rows = self.conn.execute(
            "SELECT campaign_id, summary, created_at FROM summaries WHERE campaign_id = ? ORDER BY created_at DESC LIMIT ?",
            (campaign_id, limit),
        ).fetchall()
        return [
            SummaryDBModel(
                campaign_id=row["campaign_id"],
                summary=row["summary"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def add_memory(
        self,
        campaign_id: str,
        kind: str,
        content: str,
        *,
        importance: float = 0.5,
        source_event_id: str | None = None,
        embedding: list[float] | None = None,
    ) -> MemoryDBModel:
        memory_id = f"memory_{uuid4().hex}"
        created_at = datetime.utcnow().isoformat()
        embedding_json = serialize_embedding(embedding or build_embedding(content))
        self.conn.execute(
            "INSERT INTO memories (memory_id, campaign_id, kind, content, embedding_json, importance, source_event_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (memory_id, campaign_id, kind, content, embedding_json, importance, source_event_id, created_at),
        )
        return MemoryDBModel(
            memory_id=memory_id,
            campaign_id=campaign_id,
            kind=kind,
            content=content,
            embedding_json=embedding_json,
            importance=importance,
            source_event_id=source_event_id,
            created_at=datetime.fromisoformat(created_at),
        )

    def list_campaign_memories(self, campaign_id: str, limit: int = 20) -> list[MemoryDBModel]:
        rows = self.conn.execute(
            "SELECT memory_id, campaign_id, kind, content, embedding_json, importance, source_event_id, created_at FROM memories WHERE campaign_id = ? ORDER BY created_at DESC LIMIT ?",
            (campaign_id, limit),
        ).fetchall()
        return [
            MemoryDBModel(
                memory_id=row["memory_id"],
                campaign_id=row["campaign_id"],
                kind=row["kind"],
                content=row["content"],
                embedding_json=row["embedding_json"],
                importance=float(row["importance"] or 0.0),
                source_event_id=row["source_event_id"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def search_campaign_memories(self, campaign_id: str, query: str, limit: int = 4) -> list[MemoryDBModel]:
        memories = self.list_campaign_memories(campaign_id, limit=50)
        if not memories:
            return []

        query_embedding = build_embedding(query)

        scored_memories: list[tuple[float, MemoryDBModel]] = []
        for memory in memories:
            memory_embedding = deserialize_embedding(memory.embedding_json)
            similarity = cosine_similarity(query_embedding, memory_embedding)
            score = similarity + float(memory.importance) * 0.1
            scored_memories.append((score, memory))

        scored_memories.sort(key=lambda item: (item[0], item[1].created_at), reverse=True)
        return [memory for _, memory in scored_memories[:limit]]

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
