from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from app.game.world import normalize_identifier

PLAYER_INVENTORY_LOCATION = "player:current"

STARTING_INVENTORY_SIZE = 3


@dataclass(frozen=True)
class Item:
    id: str
    name: str
    description: str
    location: str
    portable: bool = True
    quantity: int = 1
    tags: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    properties: dict[str, Any] = field(default_factory=dict)


def room_location(room_id: str) -> str:
    return f"room:{room_id}"


def build_development_items() -> dict[str, Item]:
    items = [
        Item(
            id="brass_key",
            name="Brass Key",
            description="A tarnished brass key with a long barrel and worn teeth.",
            location=room_location("library"),
            portable=True,
            tags=["key"],
            aliases=["brass key", "tarnished brass key"],
            properties={"opens": "cellar_door"},
        ),
        Item(
            id="old_book",
            name="Old Book",
            description="A mold-speckled book bound in cracked leather.",
            location=room_location("library"),
            portable=True,
            tags=["book"],
            aliases=["old book", "moldy book"],
        ),
        Item(
            id="heavy_statue",
            name="Heavy Statue",
            description="A waist-high stone statue that is far too heavy to carry.",
            location=room_location("entry_hall"),
            portable=False,
            tags=["statue"],
            aliases=["heavy statue", "stone statue"],
        ),
        Item(
            id="candle",
            name="Candle",
            description="A thin tallow candle burned halfway down.",
            location=room_location("dining_room"),
            portable=True,
            tags=["light"],
            aliases=["tallow candle"],
        ),
    ]
    return {item.id: item for item in items}


DEFAULT_ITEMS = build_development_items()


def build_starting_inventory_pool() -> dict[str, Item]:
    items = [
        Item(
            id="waterskin",
            name="Waterskin",
            description="A leather waterskin, still mostly full.",
            location=PLAYER_INVENTORY_LOCATION,
            portable=True,
            tags=["supply"],
            aliases=["waterskin", "water skin"],
        ),
        Item(
            id="coil_of_rope",
            name="Coil of Rope",
            description="A sturdy coil of rope, useful for climbing or binding.",
            location=PLAYER_INVENTORY_LOCATION,
            portable=True,
            tags=["tool"],
            aliases=["rope", "coil of rope"],
        ),
        Item(
            id="box_of_matches",
            name="Box of Matches",
            description="A small box of matches, a few sticks left inside.",
            location=PLAYER_INVENTORY_LOCATION,
            portable=True,
            tags=["light", "supply"],
            aliases=["matches", "box of matches"],
        ),
        Item(
            id="pocket_knife",
            name="Pocket Knife",
            description="A worn pocket knife with a folding blade.",
            location=PLAYER_INVENTORY_LOCATION,
            portable=True,
            tags=["tool"],
            aliases=["knife", "pocket knife"],
        ),
        Item(
            id="hand_mirror",
            name="Hand Mirror",
            description="A small hand mirror with a cracked corner.",
            location=PLAYER_INVENTORY_LOCATION,
            portable=True,
            tags=["tool"],
            aliases=["mirror", "hand mirror"],
        ),
        Item(
            id="leather_gloves",
            name="Leather Gloves",
            description="A pair of scuffed leather gloves.",
            location=PLAYER_INVENTORY_LOCATION,
            portable=True,
            tags=["wearable"],
            aliases=["gloves", "leather gloves"],
        ),
        Item(
            id="worn_journal",
            name="Worn Journal",
            description="A traveler's journal, most pages left blank.",
            location=PLAYER_INVENTORY_LOCATION,
            portable=True,
            tags=["book"],
            aliases=["journal", "worn journal"],
        ),
        Item(
            id="tinderbox",
            name="Tinderbox",
            description="A compact tinderbox for striking a quick flame.",
            location=PLAYER_INVENTORY_LOCATION,
            portable=True,
            tags=["light", "supply"],
            aliases=["tinderbox"],
        ),
    ]
    return {item.id: item for item in items}


STARTING_INVENTORY_POOL = build_starting_inventory_pool()


def default_items_state() -> dict[str, dict[str, Any]]:
    return {item_id: _item_to_state(item) for item_id, item in DEFAULT_ITEMS.items()}


def random_starting_inventory_items(
    count: int = STARTING_INVENTORY_SIZE,
) -> dict[str, dict[str, Any]]:
    """Pick a random subset of the starting-gear pool for a brand new campaign."""
    candidates = list(STARTING_INVENTORY_POOL.values())
    selected = random.sample(candidates, k=min(count, len(candidates)))
    return {item.id: _item_to_state(item) for item in selected}


def ensure_items_state(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    items_raw = state.get("items")
    defaults = default_items_state()
    normalized: dict[str, dict[str, Any]] = {}

    if isinstance(items_raw, dict):
        for item_id, raw_item in items_raw.items():
            if not isinstance(item_id, str) or not isinstance(raw_item, dict):
                continue
            normalized[item_id] = _normalize_item_state(item_id, raw_item)

    for item_id, default_item in defaults.items():
        if item_id not in normalized:
            normalized[item_id] = default_item
        else:
            merged = dict(default_item)
            merged.update(normalized[item_id])
            normalized[item_id] = _normalize_item_state(item_id, merged)

    state["items"] = normalized
    _migrate_legacy_inventory(state, normalized)
    sync_inventory_projection(state, normalized)
    return normalized


def sync_inventory_projection(
    state: dict[str, Any],
    items: dict[str, dict[str, Any]],
) -> None:
    player = state.setdefault("player", {})
    if not isinstance(player, dict):
        player = {}
        state["player"] = player

    inventory_ids = [
        item_id
        for item_id, item in items.items()
        if item.get("location") == PLAYER_INVENTORY_LOCATION and int(item.get("quantity", 1)) > 0
    ]
    player["inventory"] = inventory_ids


def inventory_item_ids(items: dict[str, dict[str, Any]]) -> list[str]:
    return [
        item_id
        for item_id, item in items.items()
        if item.get("location") == PLAYER_INVENTORY_LOCATION and int(item.get("quantity", 1)) > 0
    ]


def room_item_ids(items: dict[str, dict[str, Any]], room_id: str) -> list[str]:
    room_ref = room_location(room_id)
    return [
        item_id
        for item_id, item in items.items()
        if item.get("location") == room_ref and int(item.get("quantity", 1)) > 0
    ]


def available_items_for_room(
    items: dict[str, dict[str, Any]], room_id: str
) -> list[dict[str, str]]:
    available: list[dict[str, str]] = []
    for item_id in room_item_ids(items, room_id):
        item = items[item_id]
        name = item.get("name")
        if isinstance(name, str):
            available.append({"id": item_id, "name": name})
    return available


def resolve_item_ids(
    items: dict[str, dict[str, Any]],
    requested_target: str | None,
) -> list[str]:
    if not isinstance(requested_target, str):
        return []

    requested = normalize_identifier(requested_target)
    if not requested:
        return []

    matches: list[str] = []
    for item_id, item in items.items():
        if requested in _candidate_identifiers(item_id, item):
            matches.append(item_id)

    return matches


def _item_to_state(item: Item) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.name,
        "description": item.description,
        "location": item.location,
        "portable": item.portable,
        "quantity": item.quantity,
        "tags": list(item.tags),
        "aliases": list(item.aliases),
        "properties": dict(item.properties),
    }


def _normalize_item_state(item_id: str, raw_item: dict[str, Any]) -> dict[str, Any]:
    name = raw_item.get("name")
    description = raw_item.get("description")
    location = raw_item.get("location")
    portable = raw_item.get("portable")
    quantity = raw_item.get("quantity")
    tags = raw_item.get("tags")
    aliases = raw_item.get("aliases")
    properties = raw_item.get("properties")

    return {
        "id": item_id,
        "name": name if isinstance(name, str) and name else item_id,
        "description": description if isinstance(description, str) else "",
        "location": location if isinstance(location, str) and location else "none",
        "portable": bool(portable) if isinstance(portable, bool) else True,
        "quantity": max(1, int(quantity)) if isinstance(quantity, int) else 1,
        "tags": [tag for tag in tags if isinstance(tag, str)] if isinstance(tags, list) else [],
        "aliases": [alias for alias in aliases if isinstance(alias, str)] if isinstance(aliases, list) else [],
        "properties": properties if isinstance(properties, dict) else {},
    }


def _candidate_identifiers(item_id: str, item: dict[str, Any]) -> set[str]:
    candidates = {normalize_identifier(item_id)}

    name = item.get("name")
    if isinstance(name, str):
        normalized_name = normalize_identifier(name)
        if normalized_name:
            candidates.add(normalized_name)

    aliases = item.get("aliases")
    if isinstance(aliases, list):
        for alias in aliases:
            if isinstance(alias, str):
                normalized_alias = normalize_identifier(alias)
                if normalized_alias:
                    candidates.add(normalized_alias)

    tags = item.get("tags")
    if isinstance(tags, list):
        for tag in tags:
            if isinstance(tag, str):
                normalized_tag = normalize_identifier(tag)
                if normalized_tag:
                    candidates.add(normalized_tag)

    return candidates


def _migrate_legacy_inventory(
    state: dict[str, Any],
    items: dict[str, dict[str, Any]],
) -> None:
    player = state.get("player")
    if not isinstance(player, dict):
        return

    inventory = player.get("inventory")
    if not isinstance(inventory, list):
        return

    for raw_item in inventory:
        if not isinstance(raw_item, str):
            continue
        matches = resolve_item_ids(items, raw_item)
        if len(matches) != 1:
            continue
        items[matches[0]]["location"] = PLAYER_INVENTORY_LOCATION
