from __future__ import annotations

from app.db.models import MemoryDBModel, SummaryDBModel


class MemoryRepository:
    def __init__(self, db) -> None:
        self.db = db

    def _campaign_belongs_to_player(self, *, player_id: str, campaign_id: str) -> bool:
        return self.db.get_player_campaign(player_id, campaign_id) is not None

    def get_latest_summary(self, *, player_id: str, campaign_id: str) -> SummaryDBModel | None:
        if not self._campaign_belongs_to_player(player_id=player_id, campaign_id=campaign_id):
            return None
        return self.db.get_latest_summary(campaign_id)

    def get_campaign_state(self, *, player_id: str, campaign_id: str) -> str:
        campaign = self.db.get_player_campaign(player_id, campaign_id)
        if campaign is None:
            return "No campaign state yet."
        return campaign.state or "No campaign state yet."

    def load_recent_turns(self, *, player_id: str, campaign_id: str, limit: int) -> list[dict[str, str]]:
        if not self._campaign_belongs_to_player(player_id=player_id, campaign_id=campaign_id):
            return []
        _, turns, _ = self.db.get_campaign_with_turns(campaign_id, limit=limit)
        return [{"role": turn.role, "content": turn.content} for turn in turns]

    def search_campaign_memories(
        self,
        *,
        player_id: str,
        campaign_id: str,
        query: str,
        limit: int,
    ) -> list[MemoryDBModel]:
        if not self._campaign_belongs_to_player(player_id=player_id, campaign_id=campaign_id):
            return []
        return self.db.search_campaign_memories(campaign_id, query, limit=limit)

    def count_campaign_turns(self, *, player_id: str, campaign_id: str) -> int:
        return self.db.count_campaign_turns(player_id, campaign_id)

    def add_summary(self, *, player_id: str, campaign_id: str, summary: str) -> None:
        if not self._campaign_belongs_to_player(player_id=player_id, campaign_id=campaign_id):
            return
        self.db.add_summary(campaign_id, summary)

    def add_memory(
        self,
        *,
        player_id: str,
        campaign_id: str,
        kind: str,
        content: str,
        importance: float,
        source_event_id: str | None,
    ) -> None:
        if not self._campaign_belongs_to_player(player_id=player_id, campaign_id=campaign_id):
            return
        self.db.add_memory(
            campaign_id,
            kind,
            content,
            importance=importance,
            source_event_id=source_event_id,
        )
