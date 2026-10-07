from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from app.schemas.traversal import LocalTraversalRoute, RouteKind, TraversalRoute


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
    traversal_routes: tuple[TraversalRoute, ...] = ()

    def __post_init__(self) -> None:
        seen: set[str] = set()
        counts: dict[str, int] = {}
        for route in self.traversal_routes:
            if route.route_id in seen:
                raise ValueError("Authored traversal route ids must be unique.")
            if route.origin not in self.rooms or route.destination not in self.rooms:
                raise ValueError("Authored traversal endpoints must exist in this world.")
            seen.add(route.route_id)
            counts[route.origin] = counts.get(route.origin, 0) + 1
            if counts[route.origin] > 6:
                raise ValueError("At most six discoverable traversal routes per room are supported.")

    def route_references(self, route: TraversalRoute) -> frozenset[str]:
        return frozenset(
            normalize_identifier(value)
            for value in (
                route.route_id, route.name, route.destination,
                self.rooms[route.destination].name,
            )
        )

    def local_traversal_routes(self, room_id: str) -> list[LocalTraversalRoute]:
        return [
            LocalTraversalRoute(
                **route.model_dump(exclude={"origin"}),
                destination_name=self.rooms[route.destination].name,
            )
            for route in sorted(self.traversal_routes, key=lambda route: route.route_id)
            if route.origin == room_id
        ]

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
                "south": "rain_court",
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
        "rain_court": Room(
            id="rain_court",
            name="Rain Court",
            description="An open court beside a flooded channel. A stair and covered bridge offer ordinary access to both landings.",
            exits={"north": "dining_room", "stairs": "upper_gallery", "bridge": "far_bank"},
        ),
        "upper_gallery": Room(
            id="upper_gallery",
            name="Upper Gallery",
            description="A dry stone gallery above the court, with a safe stair back down.",
            exits={"stairs": "rain_court"},
        ),
        "far_bank": Room(
            id="far_bank",
            name="Far Bank",
            description="A stable landing beyond the channel. The covered bridge leads back to the court.",
            exits={"bridge": "rain_court"},
        ),
    }
    routes = (
        TraversalRoute(
            route_id="gallery_ascent", name="Gallery Ascent", origin="rain_court",
            destination="upper_gallery", kind=RouteKind.VERTICAL, distance_metres=2,
            clear_vertical_path=True, continuous_support=True,
            valid_takeoff=True, valid_landing=True,
            description="A clear two-metre vertical ascent to a stable gallery landing, beside a continuous stone wall. Not a walking exit.",
        ),
        TraversalRoute(
            route_id="ivy_wall", name="Ivy Wall", origin="rain_court",
            destination="upper_gallery", kind=RouteKind.SURFACE, distance_metres=5,
            continuous_support=True,
            valid_takeoff=True, valid_landing=True,
            description="A five-metre winding continuous supporting wall reaches the gallery. It is not a clear vertical path or a jumping gap.",
        ),
        TraversalRoute(
            route_id="channel_gap", name="Channel Gap", origin="rain_court",
            destination="far_bank", kind=RouteKind.GAP, distance_metres=3,
            valid_takeoff=True, valid_landing=True,
            description="A three-metre gap between stable takeoff and landing stones. No continuous climb support or vertical path.",
        ),
        TraversalRoute(
            route_id="water_crossing", name="Water Crossing", origin="rain_court",
            destination="far_bank", kind=RouteKind.WATER, distance_metres=6, water_surface=True,
            valid_takeoff=True, valid_landing=True,
            description="A six-metre water-surface crossing between dry stable landings. No underwater access or hazard immunity.",
        ),
        TraversalRoute(
            route_id="wide_gap", name="Wide Gap", origin="rain_court",
            destination="far_bank", kind=RouteKind.GAP, distance_metres=5,
            valid_takeoff=True, valid_landing=True,
            description="A five-metre gap between stable stones: beyond a supernatural jump's three-metre reach. Use the ordinary bridge instead.",
        ),
    )
    return World(rooms=rooms, traversal_routes=routes)


DEFAULT_WORLD = build_development_world()