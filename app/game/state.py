from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class RoomState:
    room_id: str
    description: str
    items: Optional[list[str]] = None


@dataclass
class PlayerState:
    player_id: str
    name: str
    current_room: str
    inventory: Optional[list[str]] = None


@dataclass
class CampaignState:
    campaign_id: str
    player: PlayerState
    current_room: RoomState
    variables: Optional[dict[str, Any]] = None
