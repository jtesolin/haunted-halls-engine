"""Read-only exploration journal projection (player-known knowledge only)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class ExplorationNode(BaseModel):
    # Visited rooms use their stable room id; unexplored destinations are
    # anonymous `frontier-*` nodes with no name.
    id: str
    kind: Literal["room", "frontier"]
    name: str | None = None


class ExplorationEdge(BaseModel):
    # Directed. Stable across revisits; never duplicated.
    id: str
    source: str
    target: str
    kind: Literal["exit", "route", "passage"]
    label: str
    traversed: bool


class ExplorationLocation(BaseModel):
    id: str
    name: str


class ExplorationResponse(BaseModel):
    current_location: ExplorationLocation
    nodes: list[ExplorationNode]
    edges: list[ExplorationEdge]
    # Flat natural-language details of the current room, projected from the same
    # player-observable scene the Narrator receives.
    observations: list[str]
