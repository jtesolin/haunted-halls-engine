"""Shared authoritative campaign-lifecycle state construction.

This is the single source of truth for building a brand new campaign's starting
state. Both campaign creation and the legacy missing-state fallback in
`ToolExecutor` must reuse this initializer rather than owning separate copies.
"""

from __future__ import annotations

import json
from typing import Any

from app.game.items import (
    default_items_state,
    ensure_items_state,
    random_starting_inventory_items,
    sync_inventory_projection,
)
from app.game.npcs import default_npcs_state, ensure_npcs_state


class InvalidCampaignStateError(Exception):
    """Raised when persisted campaign state is present but malformed.

    This deliberately does not include the raw persisted payload in its
    message so that malformed/corrupted state is never leaked into logs or
    error responses.
    """


def validate_persisted_campaign_state_json(campaign_state: str) -> None:
    """Raise `InvalidCampaignStateError` for non-empty malformed persisted state.

    This is the single shared strict-decode contract used by every layer
    (orchestrator, tool executor, narrator scene projection) that consumes
    persisted campaign state text. The legitimate empty / sentinel
    "No campaign state yet." value is treated as compatible and does not
    raise; any other non-object JSON payload is treated as corruption.
    """
    if not campaign_state or campaign_state == "No campaign state yet.":
        return
    try:
        value = json.loads(campaign_state)
    except json.JSONDecodeError as exc:
        raise InvalidCampaignStateError(
            "Persisted campaign state could not be decoded as JSON."
        ) from exc
    if not isinstance(value, dict):
        raise InvalidCampaignStateError(
            "Persisted campaign state did not decode to an object."
        )


def load_authoritative_campaign_state(campaign_state: str) -> dict[str, Any]:
    """Parse persisted campaign state text into the one canonical
    authoritative representation.

    This is the single shared decode/normalize contract used by every layer
    that consumes persisted campaign state text as full authoritative state
    (currently the player `ToolExecutor` and the Director orchestration
    step). The legitimate empty / missing-state sentinel value
    ("No campaign state yet.") produces a freshly initialized campaign via
    `build_fresh_campaign_state()`. Any other non-empty malformed or
    non-object JSON payload raises `InvalidCampaignStateError` via
    `validate_persisted_campaign_state_json()` rather than being silently
    repaired. Persisted item/NPC state is normalized with
    `ensure_items_state()` / `ensure_npcs_state()` so every caller reasons
    over the same shape.
    """
    if not campaign_state or campaign_state == "No campaign state yet.":
        return build_fresh_campaign_state()
    validate_persisted_campaign_state_json(campaign_state)
    value: dict[str, Any] = json.loads(campaign_state)
    ensure_items_state(value)
    ensure_npcs_state(value)
    return value


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
