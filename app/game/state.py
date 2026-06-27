from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class RoomState:
    room_id: str
    description: str
    items: list[str] = None


@dataclass
class PlayerState:
    player_id: str
    name: str
    current_room: str
    inventory: list[str] = None


@dataclass
class CampaignState:
    campaign_id: str
    player: PlayerState
    current_room: RoomState
    variables: dict[str, Any] = None
