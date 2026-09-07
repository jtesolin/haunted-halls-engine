"""Deterministic narrator-facing projection of the player-observable current scene.

Authoritative game state -> this projection -> observable current scene (+ ToolExecutionResult) -> Narrator.
This module never calls a model; it only reads authoritative campaign state.
"""

from __future__ import annotations

import json
from typing import Any

from app.game.items import (
    ensure_items_state,
    inventory_narrator_items,
    nearby_narrator_items_for_room,
)
from app.game.npcs import ensure_npcs_state, nearby_npcs_for_room
from app.game.world import DEFAULT_WORLD, World
from app.schemas.chat import NarratorRoom, NarratorSceneContext


def build_narrator_scene_context(
    campaign_state: str, *, world: World = DEFAULT_WORLD
) -> NarratorSceneContext:
    state = _parse_campaign_state(campaign_state)

    items = ensure_items_state(state)
    npcs = ensure_npcs_state(state)

    player = state.get("player")
    current_room_id = player.get("location") if isinstance(player, dict) else None
    room = world.get_room(current_room_id) if isinstance(current_room_id, str) else None

    if room is None:
        return NarratorSceneContext(
            current_room=NarratorRoom(),
            available_exits=[],
            nearby_items=[],
            inventory_items=inventory_narrator_items(items),
            nearby_npcs=[],
        )

    return NarratorSceneContext(
        current_room=NarratorRoom(id=room.id, name=room.name, description=room.description),
        available_exits=world.available_exits(room.id),
        nearby_items=nearby_narrator_items_for_room(items, room.id),
        inventory_items=inventory_narrator_items(items),
        nearby_npcs=nearby_npcs_for_room(npcs, room.id),
    )


def _parse_campaign_state(campaign_state: str) -> dict[str, Any]:
    if not campaign_state or campaign_state == "No campaign state yet.":
        return {}
    try:
        value = json.loads(campaign_state)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}
