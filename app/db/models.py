from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional


@dataclass
class CampaignDBModel:
    campaign_id: str
    player_id: str
    name: str
    description: Optional[str]
    state: Optional[str]
    created_at: datetime


@dataclass
class CharacterDBModel:
    character_id: str
    player_id: str
    campaign_id: str
    name: str
    description: Optional[str]
    created_at: datetime


@dataclass
class TurnDBModel:
    turn_id: str
    player_id: str
    campaign_id: str
    role: str
    content: str
    created_at: datetime


@dataclass
class GameEventDBModel:
    event_id: str
    player_id: str
    campaign_id: str
    turn_id: Optional[str]
    type: str
    payload_json: Optional[str]
    created_at: datetime


@dataclass
class SummaryDBModel:
    campaign_id: str
    summary: str
    created_at: datetime
