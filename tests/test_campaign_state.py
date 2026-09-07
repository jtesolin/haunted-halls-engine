from __future__ import annotations

from app.game.campaign_state import build_fresh_campaign_state
from app.game.items import PLAYER_INVENTORY_LOCATION, STARTING_INVENTORY_SIZE


def test_build_fresh_campaign_state_player_starts_in_entry_hall() -> None:
    state = build_fresh_campaign_state()

    assert state["player"]["location"] == "entry_hall"


def test_build_fresh_campaign_state_clock_and_facts_are_reset() -> None:
    state = build_fresh_campaign_state()

    assert state["clock"]["tick"] == 0
    assert state["facts"] == []


def test_build_fresh_campaign_state_includes_fixed_items_and_npcs() -> None:
    state = build_fresh_campaign_state()

    assert "heavy_statue" in state["items"]
    assert "old_caretaker" in state["npcs"]


def test_build_fresh_campaign_state_selects_configured_starting_inventory_size() -> None:
    state = build_fresh_campaign_state()

    inventory_ids = state["player"]["inventory"]
    assert len(inventory_ids) == STARTING_INVENTORY_SIZE
    for item_id in inventory_ids:
        assert state["items"][item_id]["location"] == PLAYER_INVENTORY_LOCATION


def test_build_fresh_campaign_state_inventory_matches_item_locations() -> None:
    state = build_fresh_campaign_state()

    expected_inventory = {
        item_id
        for item_id, item in state["items"].items()
        if item.get("location") == PLAYER_INVENTORY_LOCATION
    }
    assert set(state["player"]["inventory"]) == expected_inventory
