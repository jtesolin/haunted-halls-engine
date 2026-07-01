from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, field_validator

from app.schemas.chat import PlayerId


class CampaignCreateRequest(BaseModel):
    player_id: PlayerId

    @field_validator("player_id")
    @classmethod
    def validate_player_id(cls, value: str) -> str:
        if value.lower() == "anonymous":
            raise ValueError("player_id cannot be 'anonymous'")
        return value


class CampaignTurn(BaseModel):
    turn_id: str
    player_id: str
    role: str
    content: str
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
    name: str
    last_message: Optional[str] = None
