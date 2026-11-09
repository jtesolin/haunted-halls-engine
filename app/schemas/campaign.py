from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict


class CampaignCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CampaignTurn(BaseModel):
    turn_id: str
    role: str
    content: str
    created_at: datetime


class CampaignDetail(BaseModel):
    campaign_id: str
    name: str
    description: Optional[str] = None
    messages: List[CampaignTurn]
    truncated: bool
    # Persisted campaign->character association; null when the campaign has no
    # character row. Use with GET /api/character/{character_id}.
    character_id: Optional[str] = None


class CampaignSummary(BaseModel):
    campaign_id: str
    name: str
    last_message: Optional[str] = None
    # Persisted campaign->character association; null when none exists.
    character_id: Optional[str] = None
