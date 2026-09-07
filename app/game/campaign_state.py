"""Shared authoritative campaign-lifecycle state construction.

This is the single source of truth for building a brand new campaign's starting
state. Both campaign creation and the legacy missing-state fallback in
`ToolExecutor` must reuse this initializer rather than owning separate copies.
"""

from __future__ import annotations

from typing import Any

from app.game.items import (
    default_items_state,
    ensure_items_state,
    random_starting_inventory_items,
    sync_inventory_projection,
)
from app.game.npcs import default_npcs_state


def build_fresh_campaign_state() -> dict[str, Any]:
    """Build the authoritative starting state for a brand new campaign."""
    state: dict[str, Any] = {
        "player": {
            "location": "entry_hall",
            "inventory": [],
        },
        "items": default_items_state(),
        "npcs": {},
        "clock": {
            "tick": 0,
        },
        "facts": [],
    }
    items = ensure_items_state(state)
    state["npcs"] = default_npcs_state()
    items.update(random_starting_inventory_items())
    sync_inventory_projection(state, items)
    return state
