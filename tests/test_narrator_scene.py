from __future__ import annotations

import json

from app.game.narrator_scene import build_narrator_scene_context


def _state(**overrides: object) -> str:
    base: dict[str, object] = {
        "player": {"location": "library", "inventory": ["brass_key"]},
        "items": {
            "brass_key": {
                "location": "player:current",
                "name": "Brass Key",
                "description": "A tarnished brass key.",
                "properties": {"opens": "cellar_door"},
            },
            "old_book": {
                "location": "room:library",
                "name": "Old Book",
                "description": "A mold-speckled book.",
                "properties": {"openable": True, "is_open": True},
            },
            "candle": {
                "location": "room:dining_room",
                "name": "Candle",
                "description": "A thin tallow candle.",
                "properties": {"lightable": True, "lit": False},
            },
        },
        "npcs": {
            "library_ghost": {
                "name": "Library Ghost",
                "description": "A pale figure drifts between the shelves.",
                "location": "library",
                "status": "active",
                "disposition": "neutral",
            },
            "old_caretaker": {
                "name": "Old Caretaker",
                "description": "Watches the entry hall.",
                "location": "entry_hall",
                "status": "active",
                "disposition": "neutral",
            },
            "hidden_specter": {
                "name": "Hidden Specter",
                "description": "Lurks unseen.",
                "location": "library",
                "status": "absent",
                "disposition": "neutral",
            },
        },
    }
    base.update(overrides)
    return json.dumps(base)


def test_current_room_projected() -> None:
    scene = build_narrator_scene_context(_state())

    assert scene.current_room.id == "library"
    assert scene.current_room.name == "Library"
    assert "shelves" in (scene.current_room.description or "")


def test_exits_projected_and_deterministic() -> None:
    scene = build_narrator_scene_context(_state(player={"location": "grand_corridor", "inventory": []}))

    directions = [exit_["direction"] for exit_ in scene.available_exits]
    assert directions == sorted(directions)
    assert {"direction": "east", "room_id": "library", "room_name": "Library"} in scene.available_exits


def test_nearby_items_included_and_off_room_excluded() -> None:
    scene = build_narrator_scene_context(_state())

    item_ids = {item.id for item in scene.nearby_items}
    assert "old_book" in item_ids
    assert "candle" not in item_ids
    assert "brass_key" not in item_ids  # in inventory, not the room


def test_inventory_items_projected_separately() -> None:
    scene = build_narrator_scene_context(_state())

    inventory_ids = {item.id for item in scene.inventory_items}
    assert inventory_ids == {"brass_key"}
    nearby_ids = {item.id for item in scene.nearby_items}
    assert "brass_key" not in nearby_ids


def test_nearby_npcs_included_off_room_and_absent_excluded() -> None:
    scene = build_narrator_scene_context(_state())

    npc_ids = [npc.id for npc in scene.nearby_npcs]
    assert npc_ids == ["library_ghost"]
    assert "old_caretaker" not in npc_ids
    assert "hidden_specter" not in npc_ids


def test_npc_ordering_deterministic() -> None:
    state = _state(
        player={"location": "library", "inventory": []},
        npcs={
            "zeta_wraith": {
                "name": "Zeta Wraith",
                "description": "",
                "location": "library",
                "status": "active",
                "disposition": "neutral",
            },
            "alpha_ghost": {
                "name": "Alpha Ghost",
                "description": "",
                "location": "library",
                "status": "active",
                "disposition": "neutral",
            },
        },
    )

    scene = build_narrator_scene_context(state)

    assert [npc.id for npc in scene.nearby_npcs] == ["alpha_ghost", "zeta_wraith"]


def test_invalid_current_location_is_represented_safely() -> None:
    scene = build_narrator_scene_context(_state(player={"location": "nowhere", "inventory": ["brass_key"]}))

    assert scene.current_room.id is None
    assert scene.current_room.name is None
    assert scene.available_exits == []
    assert scene.nearby_items == []
    assert scene.nearby_npcs == []
    # Inventory remains available even without a valid current room.
    assert {item.id for item in scene.inventory_items} == {"brass_key"}


def test_observable_item_state_exposed() -> None:
    scene = build_narrator_scene_context(_state())

    old_book = next(item for item in scene.nearby_items if item.id == "old_book")
    assert old_book.observable_state == {"is_open": True}


def test_candle_lit_state_exposed() -> None:
    scene = build_narrator_scene_context(_state(player={"location": "dining_room", "inventory": []}))

    candle = next(item for item in scene.nearby_items if item.id == "candle")
    assert candle.observable_state == {"lit": False}
    assert "lightable" not in candle.observable_state


def test_capability_and_internal_properties_are_never_exposed() -> None:
    scene = build_narrator_scene_context(_state())

    old_book = next(item for item in scene.nearby_items if item.id == "old_book")
    brass_key = next(item for item in scene.inventory_items if item.id == "brass_key")

    assert "openable" not in old_book.observable_state
    assert "opens" not in brass_key.observable_state
    assert brass_key.observable_state == {}

    dumped = json.dumps(
        {
            "old_book": old_book.model_dump(),
            "brass_key": brass_key.model_dump(),
        }
    )
    assert "cellar_door" not in dumped
    assert "opens" not in dumped
    assert "openable" not in dumped
    assert "lightable" not in dumped
    assert "ignition_source" not in dumped


def test_unknown_arbitrary_properties_are_not_leaked() -> None:
    state = _state()
    payload = json.loads(state)
    payload["items"]["old_book"]["properties"]["secret_internal_flag"] = "should-not-leak"
    scene = build_narrator_scene_context(json.dumps(payload))

    old_book = next(item for item in scene.nearby_items if item.id == "old_book")
    assert "secret_internal_flag" not in old_book.observable_state


def test_malformed_observable_state_values_are_omitted() -> None:
    from app.game.items import narrator_item_projection

    malformed_book = narrator_item_projection(
        "old_book",
        {
            "name": "Old Book",
            "description": "A mold-speckled book.",
            "properties": {"is_open": "yes", "openable": True},
        },
    )
    malformed_candle = narrator_item_projection(
        "candle",
        {
            "name": "Candle",
            "description": "A thin tallow candle.",
            "properties": {"lit": {"nested": ["a" * 10_000]}, "lightable": True},
        },
    )
    malformed_list = narrator_item_projection(
        "candle",
        {
            "name": "Candle",
            "description": "A thin tallow candle.",
            "properties": {"lit": [True, False]},
        },
    )

    assert "is_open" not in malformed_book.observable_state
    assert "lit" not in malformed_candle.observable_state
    assert "lit" not in malformed_list.observable_state


def test_valid_boolean_observable_state_values_still_project() -> None:
    from app.game.items import narrator_item_projection

    projected = narrator_item_projection(
        "old_book",
        {
            "name": "Old Book",
            "description": "A mold-speckled book.",
            "properties": {"is_open": True, "lit": False, "openable": True},
        },
    )

    assert projected.observable_state == {"is_open": True, "lit": False}
