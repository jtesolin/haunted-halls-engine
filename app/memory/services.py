from __future__ import annotations

import json

from app.core.config import settings
from app.guardrails.token_budget import estimate_tokens
from app.memory.repository import MemoryRepository


class MemoryService:
    def __init__(self, db) -> None:
        self.db = db
        self.repository = MemoryRepository(db)

    def build_campaign_state(self, *, owner_user_id: str, campaign_id: str) -> str:
        return self.repository.get_campaign_state(
            owner_user_id=owner_user_id, campaign_id=campaign_id
        )

    def load_recent_turns(
        self, *, owner_user_id: str, campaign_id: str
    ) -> list[dict[str, str]]:
        return self.repository.load_recent_turns(
            owner_user_id=owner_user_id,
            campaign_id=campaign_id,
            limit=settings.MAX_RECENT_MESSAGES,
        )

    def load_memory_context(
        self,
        *,
        owner_user_id: str,
        campaign_id: str,
        query: str,
        campaign_state: str,
        recent_turns: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        memory_query = "\n".join(
            part
            for part in [
                query,
                " ".join(turn.get("content", "") for turn in recent_turns[-2:]),
            ]
            if part
        )

        memory_messages: list[dict[str, str]] = []
        total_tokens = 0
        max_tokens = settings.MAX_MEMORY_CONTEXT_TOKENS
        max_entries = settings.MEMORY_RELEVANT_ENTRIES

        latest_summary = self.repository.get_latest_summary(
            owner_user_id=owner_user_id, campaign_id=campaign_id
        )
        if latest_summary is not None:
            summary_message = {
                "role": "user",
                "content": f"Campaign summary:\n{latest_summary.summary}",
            }
            summary_tokens = estimate_tokens(summary_message.get("content", ""))
            if summary_tokens <= max_tokens and total_tokens + summary_tokens <= max_tokens:
                total_tokens += summary_tokens
                memory_messages.append(summary_message)

        memories = self.repository.search_campaign_memories(
            owner_user_id=owner_user_id,
            campaign_id=campaign_id,
            query=memory_query,
            limit=max_entries,
        )
        entries_added = 0
        for memory in memories:
            if entries_added >= max_entries:
                break

            memory_message = {
                "role": "user",
                "content": f"{memory.kind.replace('_', ' ').title()} memory:\n{memory.content}",
            }
            memory_tokens = estimate_tokens(memory_message.get("content", ""))

            if memory_tokens > max_tokens:
                continue

            if total_tokens + memory_tokens > max_tokens:
                continue

            total_tokens += memory_tokens
            memory_messages.append(memory_message)
            entries_added += 1

        return memory_messages

    def format_memory_context(self, memory_context: list[dict[str, str]]) -> str:
        return "\n".join(
            entry.get("content", "") for entry in memory_context if entry.get("content")
        )

    def maybe_store_semantic_memories(
        self,
        *,
        owner_user_id: str,
        campaign_id: str,
        user_turn_id: str,
        parsed_action,
        tool_result,
        request_message: str,
        campaign_state: str,
    ) -> None:
        if parsed_action.parse_status == "ok":
            self.repository.add_memory(
                owner_user_id=owner_user_id,
                campaign_id=campaign_id,
                kind="action",
                content=(
                    f"Player intent '{parsed_action.action}' targeting {parsed_action.target or 'nothing'} "
                    f"from message: {request_message}"
                ),
                importance=max(0.4, min(1.0, float(parsed_action.confidence))),
                source_event_id=user_turn_id,
            )

        if tool_result.success:
            self.repository.add_memory(
                owner_user_id=owner_user_id,
                campaign_id=campaign_id,
                kind="event",
                content=tool_result.summary,
                importance=0.9,
                source_event_id=user_turn_id,
            )

            if tool_result.state_delta:
                self.repository.add_memory(
                    owner_user_id=owner_user_id,
                    campaign_id=campaign_id,
                    kind="state",
                    content=f"Campaign state changed: {json.dumps(tool_result.state_delta, sort_keys=True)}",
                    importance=0.85,
                    source_event_id=user_turn_id,
                )

        if request_message:
            self.repository.add_memory(
                owner_user_id=owner_user_id,
                campaign_id=campaign_id,
                kind="message",
                content=f"Player said: {request_message}",
                importance=0.3,
                source_event_id=user_turn_id,
            )

    def should_update_summary(self, *, owner_user_id: str, campaign_id: str) -> bool:
        turn_count = self.repository.count_campaign_turns(
            owner_user_id=owner_user_id, campaign_id=campaign_id
        )
        return bool(
            turn_count > 0 and turn_count % settings.MEMORY_SUMMARY_EVERY_TURNS == 0
        )

    def should_reflect_memory(self, *, owner_user_id: str, campaign_id: str) -> bool:
        turn_count = self.repository.count_campaign_turns(
            owner_user_id=owner_user_id, campaign_id=campaign_id
        )
        return bool(
            turn_count > 0 and turn_count % settings.MEMORY_REFLECTION_EVERY_TURNS == 0
        )

    def get_current_summary_text(
        self, *, owner_user_id: str, campaign_id: str
    ) -> str | None:
        summary = self.repository.get_latest_summary(
            owner_user_id=owner_user_id, campaign_id=campaign_id
        )
        return summary.summary if summary is not None else None

    def store_summary(
        self, *, owner_user_id: str, campaign_id: str, summary_text: str
    ) -> None:
        if not summary_text.strip():
            return
        self.repository.add_summary(
            owner_user_id=owner_user_id, campaign_id=campaign_id, summary=summary_text
        )

    def store_reflection_memories(
        self,
        *,
        owner_user_id: str,
        campaign_id: str,
        source_event_id: str,
        memory_candidates,
    ) -> None:
        for candidate in memory_candidates:
            text = getattr(candidate, "text", "")
            if not text:
                continue
            importance = float(getattr(candidate, "importance", 1.0))
            memory_type = str(getattr(candidate, "memory_type", "reflection"))
            self.repository.add_memory(
                owner_user_id=owner_user_id,
                campaign_id=campaign_id,
                kind=memory_type,
                content=text,
                importance=importance,
                source_event_id=source_event_id,
            )
