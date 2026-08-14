from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any


def normalize_identifier(value: str) -> str:
    normalized = value.strip().casefold()
    normalized = normalized.replace("_", " ").replace("-", " ")
    normalized = re.sub(r"^[^a-z0-9]+|[^a-z0-9]+$", "", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    for article in ("the ", "a ", "an "):
        if normalized.startswith(article):
            normalized = normalized.removeprefix(article)
            break
    return normalized.strip()


@dataclass(frozen=True)
class Room:
    id: str
    name: str
    description: str
    exits: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class World:
    rooms: dict[str, Room]

    def get_room(self, room_id: str) -> Room | None:
        return self.rooms.get(room_id)

    def get_current_room(self, state: dict[str, Any]) -> Room | None:
        player = state.get("player") if isinstance(state, dict) else None
        if not isinstance(player, dict):
            return None

        current_room_id = player.get("location")
        if not isinstance(current_room_id, str):
            return None

        return self.get_room(current_room_id)

    def resolve_exit(self, room_id: str, direction_or_target: str | None) -> Room | None:
        current_room = self.get_room(room_id)
        if current_room is None or not direction_or_target:
            return None

        requested = normalize_identifier(direction_or_target)
        if not requested:
            return None

        for direction, exit_room_id in current_room.exits.items():
            next_room = self.get_room(exit_room_id)
            if next_room is None:
                continue

            candidates = {
                normalize_identifier(direction),
                normalize_identifier(next_room.id),
                normalize_identifier(next_room.name),
            }
            if requested in candidates:
                return next_room

        return None

    def can_move(self, room_id: str, direction_or_target: str | None) -> bool:
        return self.resolve_exit(room_id, direction_or_target) is not None

    def available_exits(self, room_id: str) -> list[dict[str, str]]:
        current_room = self.get_room(room_id)
        if current_room is None:
            return []

        exits: list[dict[str, str]] = []
        for direction, exit_room_id in sorted(current_room.exits.items()):
            next_room = self.get_room(exit_room_id)
            if next_room is None:
                continue
            exits.append(
                {
                    "direction": direction,
                    "room_id": next_room.id,
                    "room_name": next_room.name,
                }
            )
        return exits


def build_development_world() -> World:
    rooms = {
        "entry_hall": Room(
            id="entry_hall",
            name="Entry Hall",
            description="A narrow entry hall of damp stone and old brass fixtures. The air smells of dust and rain.",
            exits={
                "north": "grand_corridor",
            },
        ),
        "grand_corridor": Room(
            id="grand_corridor",
            name="Grand Corridor",
            description="A long corridor lined with faded portraits and creaking floorboards.",
            exits={
                "south": "entry_hall",
                "east": "library",
                "west": "dining_room",
                "north": "staircase",
            },
        ),
        "library": Room(
            id="library",
            name="Library",
            description="Tall shelves crowd the walls, each shelf sagging under warped books and chained ledgers.",
            exits={
                "west": "grand_corridor",
            },
        ),
        "dining_room": Room(
            id="dining_room",
            name="Dining Room",
            description="A long table sits under a cracked chandelier, set for a feast that never came.",
            exits={
                "east": "grand_corridor",
            },
        ),
        "staircase": Room(
            id="staircase",
            name="Staircase",
            description="A spiral stair climbs into shadow above the corridor.",
            exits={
                "south": "grand_corridor",
                "north": "crypt",
            },
        ),
        "crypt": Room(
            id="crypt",
            name="Crypt",
            description="A sealed lower chamber with cold stone walls and the weight of silence.",
            exits={
                "south": "staircase",
            },
        ),
    }
    return World(rooms=rooms)


DEFAULT_WORLD = build_development_world()