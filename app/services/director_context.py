from __future__ import annotations

from typing import Any

from app.game.world import DEFAULT_WORLD, World
from app.schemas.chat import ParsedAction, ToolExecutionResult
from app.schemas.director import (
    DirectorInput,
    DirectorNPCContext,
    DirectorPlayerActionContext,
)


class InvalidDirectorContextError(ValueError):
    """Raised when authoritative state cannot be projected for the Director."""


def build_director_input(
    state: dict[str, Any],
    *,
    parsed_action: ParsedAction,
    tool_result: ToolExecutionResult,
    world: World = DEFAULT_WORLD,
) -> DirectorInput:
    """Build a deterministic, non-mutating Director projection."""
    if not isinstance(state, dict):
        raise InvalidDirectorContextError("Authoritative campaign state is malformed.")

    player = state.get("player")
    if not isinstance(player, dict):
        raise InvalidDirectorContextError("Authoritative player state is malformed.")
    player_location = player.get("location")
    if not isinstance(player_location, str) or world.get_room(player_location) is None:
        raise InvalidDirectorContextError("Authoritative player location is invalid.")

    clock = state.get("clock")
    if not isinstance(clock, dict):
        raise InvalidDirectorContextError("Authoritative clock state is malformed.")
    clock_tick = clock.get("tick")
    if isinstance(clock_tick, bool) or not isinstance(clock_tick, int) or clock_tick < 0:
        raise InvalidDirectorContextError("Authoritative clock tick is invalid.")

    facts = state.get("facts")
    if not isinstance(facts, list) or any(not isinstance(fact, str) for fact in facts):
        raise InvalidDirectorContextError("Authoritative facts state is malformed.")

    npcs = state.get("npcs")
    if not isinstance(npcs, dict):
        raise InvalidDirectorContextError("Authoritative NPC state is malformed.")

    npc_ids = list(npcs)
    if any(not isinstance(npc_id, str) or not npc_id for npc_id in npc_ids):
        raise InvalidDirectorContextError("Authoritative NPC identity is malformed.")

    npc_contexts: list[DirectorNPCContext] = []
    for npc_id in sorted(npc_ids):
        npc = npcs[npc_id]
        if not isinstance(npc, dict):
            raise InvalidDirectorContextError("Authoritative NPC entry is malformed.")
        location = npc.get("location")
        status = npc.get("status")
        if not isinstance(location, str) or world.get_room(location) is None:
            raise InvalidDirectorContextError("Authoritative NPC location is invalid.")
        if status not in {"active", "absent"}:
            raise InvalidDirectorContextError("Authoritative NPC status is invalid.")
        destinations = [
            exit_data["room_id"]
            for exit_data in world.available_exits(location)
        ]
        npc_contexts.append(
            DirectorNPCContext(
                npc_id=npc_id,
                location_id=location,
                status=status,
                one_hop_destination_room_ids=destinations,
            )
        )

    return DirectorInput(
        current_player_room_id=player_location,
        clock_tick=clock_tick,
        facts=list(facts),
        npcs=npc_contexts,
        player_action=DirectorPlayerActionContext(
            action=parsed_action.action,
            target=parsed_action.target,
            parse_status=parsed_action.parse_status,
            succeeded=tool_result.success,
            result_summary=tool_result.summary,
            applied_tools=list(tool_result.applied_tools),
            error_code=tool_result.error_code,
        ),
    )
