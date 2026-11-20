"""Player-discovered exploration graph and current-room observations.

Ownership: `state["exploration"]` inside the authoritative campaign state JSON is
the only persisted exploration record (rooms the player has visited and directed
connections the player has perceived). It is written only through
`record_exploration`, in the same atomic turn write as every other state change,
so a failed turn leaves no exploration facts. Nothing here calls a model.

Knowledge boundary: a connection's destination stays unknown (`to` is None, shown
as an anonymous frontier node) until the player traverses it or sees it from a
room whose destination they have already visited. Unvisited room names/ids are
never stored or returned. Observations are not stored: they are projected on read
from the narrator's player-observable scene (`build_narrator_scene_context`), so
they track items/NPCs/state changes without a second copy that could go stale.

shortcut: exits are static authored topology, so exit edges are never removed;
revisit when worlds gain dynamic exits (locks, collapses, hidden doors) and
add a producer plus edge invalidation.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from app.game.campaign_state import (
    InvalidCampaignStateError,
    load_strict_campaign_state_for_read,
)
from app.game.narrator_scene import build_narrator_scene_context
from app.game.world import DEFAULT_WORLD, World, normalize_identifier
from app.schemas.chat import ToolExecutionResult
from app.schemas.exploration import (
    ExplorationEdge,
    ExplorationLocation,
    ExplorationNode,
    ExplorationResponse,
)

_KINDS = ("exit", "route", "passage")


def _edge_id(source: str, kind: str, key: str) -> str:
    return f"{source}/{kind}/{key}"


def _malformed() -> InvalidCampaignStateError:
    return InvalidCampaignStateError("Persisted exploration state is malformed.")


def _read(state: dict[str, Any], world: World) -> dict[str, Any] | None:
    raw = state.get("exploration")
    if raw is None:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("rooms"), list):
        raise _malformed()
    rooms, edges = raw["rooms"], raw.get("edges")
    if (
        not isinstance(edges, dict)
        or not all(isinstance(r, str) and world.get_room(r) for r in rooms)
        or len(set(rooms)) != len(rooms)
        or not isinstance(raw.get("current"), str)
        or raw["current"] not in rooms
    ):
        raise _malformed()
    for edge_id, edge in edges.items():
        if (
            not isinstance(edge_id, str)
            or not isinstance(edge, dict)
            or edge.get("from") not in rooms
            or edge.get("to") not in (*rooms, None)
            or edge.get("kind") not in _KINDS
            or not isinstance(edge.get("label"), str)
            or not isinstance(edge.get("traversed"), bool)
        ):
            raise _malformed()
        _check_edge(world, edge_id, edge)
    return raw


def _check_edge(world: World, edge_id: str, edge: dict[str, Any]) -> None:
    """Every string the API echoes (id, label) must be exactly what the engine writes."""
    source, kind, to = edge["from"], edge["kind"], edge["to"]
    prefix = f"{source}/{kind}/"
    key = edge_id.removeprefix(prefix)
    if (
        not edge_id.startswith(prefix)
        or source == to
        or (edge["traversed"] and to is None)
    ):
        raise _malformed()
    if kind == "exit":
        expected = key if key in world.rooms[source].exits else None
    elif kind == "route":
        expected = next(
            (r.name for r in world.local_traversal_routes(source) if r.route_id == key), None
        )
    else:
        expected = "unmapped passage" if to is not None and key == to else None
    if expected is None or edge["label"] != expected:
        raise _malformed()


def _traversal_edge(
    world: World, previous: str, current: str, tool_result: ToolExecutionResult | None
) -> tuple[str, str, str]:
    """(edge id, kind, label) for the movement previous -> current."""
    effect = (
        tool_result.ability_result.traversal_effect
        if tool_result is not None and tool_result.ability_result is not None
        else None
    )
    if effect is not None and effect.origin == previous and effect.destination == current:
        return _edge_id(previous, "route", effect.route_id), "route", effect.route_name
    exits = sorted(
        direction
        for direction, dest in world.rooms[previous].exits.items()
        if dest == current
    )
    if exits:
        requested = normalize_identifier(tool_result.requested_target or "") if tool_result else ""
        direction = next((d for d in exits if normalize_identifier(d) == requested), exits[0])
        return _edge_id(previous, "exit", direction), "exit", direction
    # Movement with no authored exit/route (teleport, phasing, scripted transit).
    return _edge_id(previous, "passage", current), "passage", "unmapped passage"


def record_exploration(
    state: dict[str, Any],
    *,
    world: World = DEFAULT_WORLD,
    tool_result: ToolExecutionResult | None = None,
) -> bool:
    """Fold the player's current location into the graph. True if state changed.

    A legacy campaign (no graph yet) is baselined at the tool result's
    `previous_location` when it moved, so its first move keeps its origin room.

    Idempotent; raises `InvalidCampaignStateError` on malformed persisted data.
    """
    player = state.get("player")
    current = player.get("location") if isinstance(player, dict) else None
    if not isinstance(current, str) or world.get_room(current) is None:
        return False
    before = copy.deepcopy(state.get("exploration"))
    ex = _read(state, world)
    if ex is None:
        origin = tool_result.previous_location if tool_result else None
        if not isinstance(origin, str) or world.get_room(origin) is None:
            origin = current
        ex = {"rooms": [origin], "current": origin, "edges": {}}
        state["exploration"] = ex
    visited: list[str] = ex["rooms"]
    edges: dict[str, dict[str, Any]] = ex["edges"]

    previous = ex["current"]
    if current not in visited:
        visited.append(current)
    if previous != current:
        edge_id, kind, label = _traversal_edge(world, previous, current, tool_result)
        edge = edges.setdefault(
            edge_id, {"from": previous, "to": None, "kind": kind, "label": label, "traversed": False}
        )
        edge["to"], edge["traversed"] = current, True
        ex["current"] = current

    perceived = [
        (_edge_id(current, "exit", direction), "exit", direction, dest)
        for direction, dest in sorted(world.rooms[current].exits.items())
        if world.get_room(dest) is not None
    ] + [
        (_edge_id(current, "route", r.route_id), "route", r.name, r.destination)
        for r in world.local_traversal_routes(current)
    ]
    for edge_id, kind, label, dest in perceived:
        edge = edges.setdefault(
            edge_id, {"from": current, "to": None, "kind": kind, "label": label, "traversed": False}
        )
        if edge["to"] is None and dest in visited:
            edge["to"] = dest
    return state.get("exploration") != before


def _frontier_id(edge_id: str) -> str:
    return "frontier-" + hashlib.sha256(edge_id.encode()).hexdigest()[:12]


def _observations(campaign_state: str, world: World) -> list[str]:
    scene = build_narrator_scene_context(campaign_state, world=world)
    lines: list[str] = []
    if scene.current_room.description:
        lines.append(scene.current_room.description)
    for item in scene.nearby_items:
        text = f"{item.name}: {item.description}".strip()
        if item.observable_state.get("is_open") is not None:
            text += " It is open." if item.observable_state["is_open"] else " It is closed."
        if item.observable_state.get("lit") is not None:
            text += " It is lit." if item.observable_state["lit"] else " It is unlit."
        lines.append(text)
    lines.extend(f"{n.name}: {n.description}".strip() for n in scene.nearby_npcs)
    # Neutral crossing text only: route prose and destination names stay out.
    lines.extend(
        f"{r.name}: a {r.distance_metres}-metre {r.kind.value} crossing."
        for r in scene.traversal_routes
    )
    return list(dict.fromkeys(lines))  # one entry per distinct fact, order kept


def project_exploration(campaign_state: str, *, world: World = DEFAULT_WORLD) -> ExplorationResponse:
    """Read-only journal projection. Never persists or repairs state.

    A legacy campaign with no recorded graph is projected as if just entered, which
    is exactly what its next successful turn will persist.
    """
    state = load_strict_campaign_state_for_read(campaign_state)
    # A recorded graph must already end at the authoritative location: a read never
    # synthesizes movement no turn recorded (only an absent graph is baselined).
    recorded = state.get("exploration")
    if recorded is not None:
        _read(state, world)
        if recorded["current"] != state.get("player", {}).get("location"):
            raise _malformed()
    # Compute a transient graph without touching the source.
    scratch = copy.deepcopy(state)
    record_exploration(scratch, world=world)
    ex = scratch.get("exploration")
    if ex is None:
        raise _malformed()
    here = world.get_room(ex["current"])
    if here is None:
        raise _malformed()

    nodes = [
        ExplorationNode(id=r, kind="room", name=world.rooms[r].name) for r in ex["rooms"]
    ]
    edges: list[ExplorationEdge] = []
    for edge_id, edge in sorted(ex["edges"].items()):
        target = edge["to"] or _frontier_id(edge_id)
        if edge["to"] is None:
            nodes.append(ExplorationNode(id=target, kind="frontier"))
        edges.append(
            ExplorationEdge(
                id=edge_id, source=edge["from"], target=target, kind=edge["kind"],
                label=edge["label"], traversed=edge["traversed"],
            )
        )
    return ExplorationResponse(
        current_location=ExplorationLocation(id=here.id, name=here.name),
        nodes=nodes,
        edges=edges,
        observations=_observations(json.dumps(scratch), world),
    )
