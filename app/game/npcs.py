from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.game.world import normalize_identifier
from app.schemas.chat import NearbyNPC


@dataclass(frozen=True)
class NPC:
    id: str
    name: str
    description: str
    location: str
    status: str = "active"
    disposition: str = "neutral"
    aliases: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


def build_development_npcs() -> dict[str, NPC]:
    npcs = [
        NPC(
            id="old_caretaker",
            name="Old Caretaker",
            description="An old caretaker watches the entry hall with a ring of tarnished keys at his belt.",
            location="entry_hall",
            aliases=["caretaker", "old man"],
            tags=["caretaker", "human"],
        ),
        NPC(
            id="library_ghost",
            name="Library Ghost",
            description="A pale figure drifts between the library shelves, its outline wavering like candle smoke.",
            location="library",
            aliases=["ghost", "apparition"],
            tags=["ghost", "undead"],
        ),
        NPC(
            id="crypt_warden",
            name="Crypt Warden",
            description="A silent warden stands beside the sealed crypt door in battered ceremonial armor.",
            location="crypt",
            aliases=["warden", "guard"],
            tags=["warden", "undead"],
        ),
    ]
    return {npc.id: npc for npc in npcs}


DEVELOPMENT_NPCS = build_development_npcs()


def default_npcs_state() -> dict[str, dict[str, Any]]:
    return {npc_id: _npc_to_state(npc) for npc_id, npc in DEVELOPMENT_NPCS.items()}


def ensure_npcs_state(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw_npcs = state.get("npcs")
    normalized: dict[str, dict[str, Any]] = {}
    if isinstance(raw_npcs, dict):
        for npc_id, raw_npc in raw_npcs.items():
            if not isinstance(npc_id, str) or not isinstance(raw_npc, dict):
                continue
            normalized[npc_id] = _normalize_npc_state(npc_id, raw_npc)
    state["npcs"] = normalized
    return normalized


def nearby_npcs_for_room(
    npcs: dict[str, dict[str, Any]], room_id: str
) -> list[NearbyNPC]:
    return [
        NearbyNPC(**npc_projection(npc_id, npc))
        for npc_id, npc in npcs.items()
        if npc.get("location") == room_id and npc.get("status") != "absent"
    ]


def resolve_npc_ids(
    npcs: dict[str, dict[str, Any]],
    requested_target: str | None,
    room_id: str | None = None,
) -> list[str]:
    if not isinstance(requested_target, str):
        return []
    requested = normalize_identifier(requested_target)
    if not requested:
        return []
    matches: list[str] = []
    for npc_id, npc in npcs.items():
        if room_id is not None and npc.get("location") != room_id:
            continue
        candidates = {
            normalize_identifier(npc_id),
            normalize_identifier(str(npc.get("name", ""))),
            *(normalize_identifier(str(alias)) for alias in _string_list(npc.get("aliases"))),
            *(normalize_identifier(str(tag)) for tag in _string_list(npc.get("tags"))),
        }
        if requested in candidates:
            matches.append(npc_id)
    return matches


def npc_projection(npc_id: str, npc: dict[str, Any]) -> dict[str, str]:
    return {
        "id": npc_id,
        "name": str(npc.get("name", _display_name(npc_id))),
        "description": str(npc.get("description", "")),
        "status": str(npc.get("status", "active")),
        "disposition": str(npc.get("disposition", "neutral")),
    }


def parser_npc_projection(npc_id: str, npc: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": npc_id,
        "name": str(npc.get("name", _display_name(npc_id))),
        "aliases": _string_list(npc.get("aliases")),
    }


def _npc_to_state(npc: NPC) -> dict[str, Any]:
    return {
        "id": npc.id,
        "name": npc.name,
        "description": npc.description,
        "location": npc.location,
        "status": npc.status,
        "disposition": npc.disposition,
        "aliases": list(npc.aliases),
        "tags": list(npc.tags),
    }


def _normalize_npc_state(npc_id: str, raw_npc: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        "id": npc_id,
        "name": _display_name(npc_id),
        "description": "",
        "location": "",
        "status": "active",
        "disposition": "neutral",
        "aliases": [],
        "tags": [],
    }
    normalized.update(raw_npc)
    if isinstance(raw_npc.get("room"), str) and not isinstance(raw_npc.get("location"), str):
        normalized["location"] = raw_npc["room"]
    normalized["id"] = npc_id
    for key in ("name", "description", "location", "status", "disposition"):
        if not isinstance(normalized[key], str):
            normalized[key] = {
                "name": _display_name(npc_id),
                "description": "",
                "location": "",
                "status": "active",
                "disposition": "neutral",
            }[key]
    normalized["aliases"] = _string_list(normalized.get("aliases"))
    normalized["tags"] = _string_list(normalized.get("tags"))
    normalized.pop("room", None)
    return normalized


def _display_name(npc_id: str) -> str:
    return " ".join(part.capitalize() for part in npc_id.replace("-", "_").split("_"))


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, str)]