from __future__ import annotations

import json
from datetime import datetime
from datetime import timezone
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import and_, case, delete, func, insert, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.db.models import (
    CampaignDBModel,
    CharacterDBModel,
    GameEventDBModel,
    InternalUserDBModel,
    MemoryDBModel,
    SummaryDBModel,
    TurnDBModel,
)
from app.db.schema import (
    campaigns,
    characters,
    game_events,
    internal_users,
    memories,
    model_requests,
    summaries,
    turns,
)
from app.memory.retriever import (
    build_embedding,
    cosine_similarity,
    deserialize_embedding,
    serialize_embedding,
)
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

    def get_internal_user_by_identity(
        self, provider_issuer: str, provider_subject: str
    ) -> InternalUserDBModel | None:
        stmt = select(internal_users).where(
            and_(
                internal_users.c.provider_issuer == provider_issuer,
                internal_users.c.provider_subject == provider_subject,
            )
        )
        row = self.conn.execute(stmt).mappings().first()
        if row is None:
            return None
        return self._row_to_internal_user(row)

    def get_internal_user_by_id(self, user_id: str) -> InternalUserDBModel | None:
        stmt = select(internal_users).where(internal_users.c.user_id == user_id)
        row = self.conn.execute(stmt).mappings().first()
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
            insert(internal_users).values(
                user_id=user_id,
                identity_provider=identity_provider,
                provider_issuer=provider_issuer,
                provider_subject=provider_subject,
                email=email,
                email_verified=email_verified,
                display_name=display_name,
                avatar_url=avatar_url,
                created_at=created_at,
                updated_at=created_at,
                last_login_at=created_at,
            )
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
            update(internal_users)
            .where(internal_users.c.user_id == user_id)
            .values(
                email=email,
                email_verified=email_verified,
                display_name=display_name,
                avatar_url=avatar_url,
                updated_at=now_iso,
                last_login_at=now_iso,
            )
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
            refreshed = self.get_internal_user_by_identity(
                provider_issuer, provider_subject
            )
            if refreshed is None:
                raise RuntimeError("internal user disappeared during update")
            return refreshed

        user_id = f"user_{uuid4().hex}"
        try:
            with self.conn.begin_nested():
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
        except IntegrityError:
            try:
                with self.conn.begin_nested():
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
            except IntegrityError:
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
        refreshed = self.get_internal_user_by_identity(
            provider_issuer, provider_subject
        )
        if refreshed is None:
            raise RuntimeError("failed to reload resolved internal user")
        return refreshed

    def create_campaign(
        self,
        campaign_id: str,
        owner_user_id: str,
        name: str,
        description: Optional[str] = None,
        state: Optional[dict[str, Any]] = None,
    ) -> CampaignDBModel:
        created_at = datetime.utcnow().isoformat()
        state_json = json.dumps(state) if state is not None else None
        try:
            with self.conn.begin_nested():
                self.conn.execute(
                    insert(campaigns).values(
                        campaign_id=campaign_id,
                        owner_user_id=owner_user_id,
                        name=name,
                        description=description,
                        state=state_json,
                        created_at=created_at,
                    )
                )
        except IntegrityError:
            pass
        return CampaignDBModel(
            campaign_id,
            name,
            description,
            state_json,
            datetime.fromisoformat(created_at),
            owner_user_id=owner_user_id,
        )

    def _row_to_campaign(self, row) -> CampaignDBModel:
        return CampaignDBModel(
            campaign_id=row["campaign_id"],
            name=row["name"],
            description=row["description"],
            state=row["state"],
            created_at=datetime.fromisoformat(row["created_at"]),
            owner_user_id=row["owner_user_id"],
        )

    def get_campaign(self, campaign_id: str) -> Optional[CampaignDBModel]:
        row = self.conn.execute(
            select(campaigns).where(campaigns.c.campaign_id == campaign_id)
        ).mappings().first()
        if row is None:
            return None
        return self._row_to_campaign(row)

    def update_campaign_state(self, campaign_id: str, state: dict[str, Any]) -> None:
        self.conn.execute(
            update(campaigns)
            .where(campaigns.c.campaign_id == campaign_id)
            .values(state=json.dumps(state))
        )

    def count_owner_campaigns(self, owner_user_id: str) -> int:
        total = self.conn.execute(
            select(func.count()).select_from(campaigns).where(
                campaigns.c.owner_user_id == owner_user_id
            )
        ).scalar_one()
        return int(total or 0)

    def list_campaigns_for_owner(
        self, owner_user_id: str
    ) -> list[dict[str, Optional[str]]]:
        last_message = (
            select(turns.c.content)
            .where(and_(turns.c.campaign_id == campaigns.c.campaign_id, turns.c.role == "assistant"))
            .order_by(turns.c.created_at.desc())
            .limit(1)
            .correlate(campaigns)
            .scalar_subquery()
        )
        rows = self.conn.execute(
            select(campaigns.c.campaign_id, campaigns.c.name, last_message.label("last_message"))
            .where(campaigns.c.owner_user_id == owner_user_id)
            .order_by(campaigns.c.created_at.desc())
        ).mappings().all()
        return [
            {
                "campaign_id": row["campaign_id"],
                "name": row["name"],
                "last_message": row["last_message"],
            }
            for row in rows
        ]

    def get_campaign_for_owner(
        self, campaign_id: str, owner_user_id: str
    ) -> Optional[CampaignDBModel]:
        row = self.conn.execute(
            select(campaigns).where(
                and_(campaigns.c.campaign_id == campaign_id, campaigns.c.owner_user_id == owner_user_id)
            )
        ).mappings().first()
        if row is None:
            return None
        return self._row_to_campaign(row)

    def delete_campaign_for_owner(self, campaign_id: str, owner_user_id: str) -> bool:
        result = self.conn.execute(
            delete(campaigns).where(
                and_(campaigns.c.campaign_id == campaign_id, campaigns.c.owner_user_id == owner_user_id)
            )
        )
        return result.rowcount > 0

    def get_campaign_with_turns_for_owner(
        self, campaign_id: str, owner_user_id: str, limit: int = 10
    ) -> tuple[Optional[CampaignDBModel], list[TurnDBModel], bool]:
        campaign = self.get_campaign_for_owner(campaign_id, owner_user_id)
        if campaign is None:
            return None, [], False
        rows = self.conn.execute(
            select(turns)
            .where(turns.c.campaign_id == campaign_id)
            .order_by(turns.c.created_at.desc())
            .limit(limit + 1)
        ).mappings().all()
        turn_models = [
            TurnDBModel(
                turn_id=row["turn_id"],
                campaign_id=row["campaign_id"],
                role=row["role"],
                content=row["content"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows[:limit]
        ]
        turn_models.reverse()
        truncated = len(rows) > limit
        return campaign, turn_models, truncated

    def get_character_for_owner(
        self, character_id: str, owner_user_id: str
    ) -> Optional[CharacterDBModel]:
        row = self.conn.execute(
            select(characters)
            .join(campaigns, characters.c.campaign_id == campaigns.c.campaign_id)
            .where(and_(characters.c.character_id == character_id, campaigns.c.owner_user_id == owner_user_id))
        ).mappings().first()
        if row is None:
            return None
        return CharacterDBModel(
            character_id=row["character_id"],
            campaign_id=row["campaign_id"],
            name=row["name"],
            description=row["description"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def list_characters_for_owner(self, owner_user_id: str) -> list[CharacterDBModel]:
        rows = self.conn.execute(
            select(characters)
            .join(campaigns, characters.c.campaign_id == campaigns.c.campaign_id)
            .where(campaigns.c.owner_user_id == owner_user_id)
            .order_by(characters.c.created_at.desc())
        ).mappings().all()
        return [
            CharacterDBModel(
                character_id=row["character_id"],
                campaign_id=row["campaign_id"],
                name=row["name"],
                description=row["description"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def count_user_requests_since(self, owner_user_id: str, since_iso: str) -> int:
        total = self.conn.execute(
            select(func.count())
            .select_from(turns)
            .join(campaigns, turns.c.campaign_id == campaigns.c.campaign_id)
            .where(
                and_(
                    campaigns.c.owner_user_id == owner_user_id,
                    turns.c.role == "user",
                    turns.c.created_at >= since_iso,
                )
            )
        ).scalar_one()
        return int(total or 0)

    def count_project_requests_since(self, since_iso: str) -> int:
        total = self.conn.execute(
            select(func.count())
            .select_from(model_requests)
            .where(model_requests.c.created_at >= since_iso)
        ).scalar_one()
        return int(total or 0)

    def _model_request_total_sql(self):
        actual_total = model_requests.c.actual_total_tokens
        actual_input = model_requests.c.actual_input_tokens
        actual_output = model_requests.c.actual_output_tokens
        estimated_input = model_requests.c.estimated_input_tokens
        estimated_output = model_requests.c.estimated_output_tokens

        effective_input = case(
            (actual_input.is_not(None), actual_input),
            else_=estimated_input,
        )
        effective_output = case(
            (actual_output.is_not(None), actual_output),
            else_=estimated_output,
        )
        return case(
            (actual_total.is_not(None), actual_total),
            else_=(effective_input + effective_output),
        )

    def sum_user_model_tokens_since(
        self, owner_user_id: str, since_iso: str
    ) -> int:
        total = self.conn.execute(
            select(func.coalesce(func.sum(self._model_request_total_sql()), 0))
            .select_from(model_requests)
            .where(
                and_(
                    model_requests.c.owner_user_id == owner_user_id,
                    model_requests.c.created_at >= since_iso,
                )
            )
        ).scalar_one()
        return int(total or 0)

    def sum_user_estimated_input_tokens_since(
        self, owner_user_id: str, since_iso: str
    ) -> int:
        return self.sum_user_model_tokens_since(owner_user_id, since_iso)

    def sum_project_model_tokens_since(self, since_iso: str) -> int:
        total = self.conn.execute(
            select(func.coalesce(func.sum(self._model_request_total_sql()), 0))
            .select_from(model_requests)
            .where(model_requests.c.created_at >= since_iso)
        ).scalar_one()
        return int(total or 0)

    def count_campaign_turns(self, campaign_id: str, owner_user_id: str) -> int:
        total = self.conn.execute(
            select(func.count())
            .select_from(turns)
            .join(campaigns, turns.c.campaign_id == campaigns.c.campaign_id)
            .where(
                and_(
                    turns.c.campaign_id == campaign_id,
                    campaigns.c.owner_user_id == owner_user_id,
                    turns.c.role == "user",
                )
            )
        ).scalar_one()
        return int(total or 0)

    def log_model_request(
        self,
        request_id: str,
        owner_user_id: str,
        campaign_id: str,
        turn_id: str,
        agent_name: str,
        model: str,
        estimated_input_tokens: int,
        estimated_output_tokens: int = settings.MAX_OUTPUT_TOKENS,
        actual_input_tokens: int | None = None,
        actual_output_tokens: int | None = None,
        cached_input_tokens: int | None = None,
        cache_write_input_tokens: int | None = None,
        reasoning_output_tokens: int | None = None,
        actual_total_tokens: int | None = None,
        latency_ms: int = 0,
        success: bool = False,
        failure_reason: str | None = None,
        cost_estimate: float | None = None,
    ) -> None:
        created_at = datetime.utcnow().isoformat()
        self.conn.execute(
            insert(model_requests).values(
                request_id=request_id,
                owner_user_id=owner_user_id,
                campaign_id=campaign_id,
                turn_id=turn_id,
                agent_name=agent_name,
                model=model,
                estimated_input_tokens=estimated_input_tokens,
                estimated_output_tokens=estimated_output_tokens,
                actual_input_tokens=actual_input_tokens,
                cached_input_tokens=cached_input_tokens,
                cache_write_input_tokens=cache_write_input_tokens,
                actual_output_tokens=actual_output_tokens,
                reasoning_output_tokens=reasoning_output_tokens,
                actual_total_tokens=actual_total_tokens,
                latency_ms=latency_ms,
                success=success,
                failure_reason=failure_reason,
                cost_estimate=cost_estimate,
                created_at=created_at,
            )
        )

    def create_turn(
        self,
        campaign_id: str,
        turn_id: str,
        role: str,
        content: str,
    ) -> TurnDBModel:
        created_at = datetime.utcnow().isoformat()
        self.conn.execute(
            insert(turns).values(
                turn_id=turn_id,
                campaign_id=campaign_id,
                role=role,
                content=content,
                created_at=created_at,
            )
        )
        return TurnDBModel(
            turn_id, campaign_id, role, content, datetime.fromisoformat(created_at)
        )

    def add_event(
        self,
        event_id: str,
        campaign_id: str,
        turn_id: str,
        type: GameEventType,
        payload: Optional[GameEventPayload] = None,
    ) -> GameEventDBModel:
        created_at = datetime.utcnow().isoformat()
        payload_json = payload.model_dump_json() if payload is not None else None
        self.conn.execute(
            insert(game_events).values(
                event_id=event_id,
                campaign_id=campaign_id,
                turn_id=turn_id,
                type=type,
                payload_json=payload_json,
                created_at=created_at,
            )
        )
        return GameEventDBModel(
            event_id,
            campaign_id,
            turn_id,
            type,
            payload_json,
            datetime.fromisoformat(created_at),
        )

    def add_summary(self, campaign_id: str, summary: str) -> SummaryDBModel:
        created_at = datetime.utcnow().isoformat()
        self.conn.execute(
            insert(summaries).values(
                campaign_id=campaign_id,
                summary=summary,
                created_at=created_at,
            )
        )
        return SummaryDBModel(campaign_id, summary, datetime.fromisoformat(created_at))

    def get_latest_summary(self, campaign_id: str) -> SummaryDBModel | None:
        row = self.conn.execute(
            select(summaries)
            .where(summaries.c.campaign_id == campaign_id)
            .order_by(summaries.c.created_at.desc())
            .limit(1)
        ).mappings().first()
        if row is None:
            return None
        return SummaryDBModel(
            campaign_id=row["campaign_id"],
            summary=row["summary"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def list_campaign_summaries(
        self, campaign_id: str, limit: int = 5
    ) -> list[SummaryDBModel]:
        rows = self.conn.execute(
            select(summaries)
            .where(summaries.c.campaign_id == campaign_id)
            .order_by(summaries.c.created_at.desc())
            .limit(limit)
        ).mappings().all()
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
            insert(memories).values(
                memory_id=memory_id,
                campaign_id=campaign_id,
                kind=kind,
                content=content,
                embedding_json=embedding_json,
                importance=importance,
                source_event_id=source_event_id,
                created_at=created_at,
            )
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

    def list_campaign_memories(
        self, campaign_id: str, limit: int = 20
    ) -> list[MemoryDBModel]:
        rows = self.conn.execute(
            select(memories)
            .where(memories.c.campaign_id == campaign_id)
            .order_by(memories.c.created_at.desc())
            .limit(limit)
        ).mappings().all()
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

    def search_campaign_memories(
        self, campaign_id: str, query: str, limit: int = 4
    ) -> list[MemoryDBModel]:
        memories_list = self.list_campaign_memories(campaign_id, limit=50)
        if not memories_list:
            return []

        query_embedding = build_embedding(query)

        scored_memories: list[tuple[float, MemoryDBModel]] = []
        for memory in memories_list:
            memory_embedding = deserialize_embedding(memory.embedding_json)
            similarity = cosine_similarity(query_embedding, memory_embedding)
            score = similarity + float(memory.importance) * 0.1
            scored_memories.append((score, memory))

        scored_memories.sort(
            key=lambda item: (item[0], item[1].created_at), reverse=True
        )
        return [memory for _, memory in scored_memories[:limit]]

    def get_campaign_with_turns(
        self, campaign_id: str, limit: int = 10
    ) -> tuple[Optional[CampaignDBModel], list[TurnDBModel], bool]:
        campaign = self.get_campaign(campaign_id)
        if campaign is None:
            return None, [], False

        rows = self.conn.execute(
            select(turns)
            .where(turns.c.campaign_id == campaign_id)
            .order_by(turns.c.created_at.desc())
            .limit(limit + 1)
        ).mappings().all()
        turn_models = [
            TurnDBModel(
                turn_id=row["turn_id"],
                campaign_id=row["campaign_id"],
                role=row["role"],
                content=row["content"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows[:limit]
        ]
        turn_models.reverse()
        truncated = len(rows) > limit
        return campaign, turn_models, truncated

    def list_campaign_events(
        self, campaign_id: str, limit: int = 100
    ) -> list[GameEventDBModel]:
        rows = self.conn.execute(
            select(game_events)
            .where(game_events.c.campaign_id == campaign_id)
            .order_by(game_events.c.created_at.asc())
            .limit(limit)
        ).mappings().all()
        return [
            GameEventDBModel(
                event_id=row["event_id"],
                campaign_id=row["campaign_id"],
                turn_id=row["turn_id"],
                type=row["type"],
                payload_json=row["payload_json"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def get_character(self, character_id: str) -> Optional[CharacterDBModel]:
        row = self.conn.execute(
            select(characters).where(characters.c.character_id == character_id)
        ).mappings().first()
        if row is None:
            return None
        return CharacterDBModel(
            character_id=row["character_id"],
            campaign_id=row["campaign_id"],
            name=row["name"],
            description=row["description"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )
