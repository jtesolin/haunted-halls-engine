from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel


class CampaignTurn(BaseModel):
    turn_id: str
    player_message: str
    ai_reply: str
    created_at: datetime


class CampaignDetail(BaseModel):
    campaign_id: str
    name: str
    description: Optional[str] = None
    player_id: Optional[str] = None
    messages: List[CampaignTurn]
    truncated: bool


class CampaignSummary(BaseModel):
    campaign_id: str
    title: str
    last_message: Optional[str] = None
