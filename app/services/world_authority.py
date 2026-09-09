from __future__ import annotations

import copy
from typing import Any

from pydantic import ValidationError

from app.game.world import DEFAULT_WORLD, World
from app.schemas.world import (
    AdvanceClockWorldAction,
    MoveNpcWorldAction,
    RecordFactWorldAction,
    SetNpcStatusWorldAction,
    WorldAction,
    WorldActionResult,
    WorldActionType,
)


class WorldAuthorityExecutor:
    def __init__(self, world: World | None = None) -> None:
        self.world = world or DEFAULT_WORLD

    def execute(self, action: WorldAction | dict[str, Any], state: dict[str, Any]) -> tuple[dict[str, Any], WorldActionResult]:
        if not isinstance(state, dict):
            return state, self._result(
                success=False,
                action="",
                summary="Campaign state is not a dictionary.",
                error_code="malformed_campaign_state",
                errors=["malformed_campaign_state"],
            )

        try:
            action_model = self._coerce_action(action)
        except ValidationError:
            return state, self._result(
                success=False,
                action="",
                summary="World action payload is malformed.",
                error_code="invalid_world_action",
                errors=["invalid_world_action"],
            )
        if action_model is None:
            return state, self._result(
                success=False,
                action="",
                summary=(
                    "Unsupported world action payload."
                    if not isinstance(action, dict)
                    else "World action payload is malformed."
                ),
                error_code="unsupported_world_action" if not isinstance(action, dict) else "invalid_world_action",
                errors=["unsupported_world_action" if not isinstance(action, dict) else "invalid_world_action"],
            )

        candidate = copy.deepcopy(state)
        if isinstance(action_model, MoveNpcWorldAction):
            return self._execute_move_npc(candidate, action_model, state)
        if isinstance(action_model, SetNpcStatusWorldAction):
            return self._execute_set_npc_status(candidate, action_model, state)
        if isinstance(action_model, AdvanceClockWorldAction):
            return self._execute_advance_clock(candidate, action_model, state)
        if isinstance(action_model, RecordFactWorldAction):
            return self._execute_record_fact(candidate, action_model, state)
        return state, self._result(
            success=False,
            action=str(getattr(action_model, "action", "unknown")),
            summary="Unsupported world action type.",
            error_code="unsupported_world_action",
            errors=["unsupported_world_action"],
        )

    def _execute_move_npc(
        self,
        candidate: dict[str, Any],
        action: MoveNpcWorldAction,
        original_state: dict[str, Any],
    ) -> tuple[dict[str, Any], WorldActionResult]:
        npcs = candidate.get("npcs")
        if not isinstance(npcs, dict):
            return original_state, self._result(
                success=False,
                action=WorldActionType.MOVE_NPC,
                summary="NPC state is malformed.",
                error_code="malformed_npcs_state",
                errors=["malformed_npcs_state"],
            )

        npc = npcs.get(action.npc_id)
        if not isinstance(npc, dict):
            return original_state, self._result(
                success=False,
                action=WorldActionType.MOVE_NPC,
                summary=f"NPC '{action.npc_id}' does not exist.",
                error_code="npc_not_found",
                errors=["npc_not_found"],
            )

        current_location = npc.get("location")
        if not isinstance(current_location, str) or self.world.get_room(current_location) is None:
            return original_state, self._result(
                success=False,
                action=WorldActionType.MOVE_NPC,
                summary=f"NPC '{action.npc_id}' is in an invalid room.",
                error_code="invalid_npc_location",
                errors=["invalid_npc_location"],
            )

        destination_room = self.world.get_room(action.destination_room_id)
        if destination_room is None:
            return original_state, self._result(
                success=False,
                action=WorldActionType.MOVE_NPC,
                summary=f"Destination room '{action.destination_room_id}' does not exist.",
                error_code="destination_room_not_found",
                errors=["destination_room_not_found"],
            )

        current_room = self.world.get_room(current_location)
        if current_room is None:
            return original_state, self._result(
                success=False,
                action=WorldActionType.MOVE_NPC,
                summary=f"Current room for NPC '{action.npc_id}' is invalid.",
                error_code="invalid_npc_location",
                errors=["invalid_npc_location"],
            )

        reachable = sum(
            1
            for exit_room_id in current_room.exits.values()
            if exit_room_id == action.destination_room_id
        )
        if reachable != 1:
            return original_state, self._result(
                success=False,
                action=WorldActionType.MOVE_NPC,
                summary=f"Room '{action.destination_room_id}' is not exactly one hop away from '{current_location}'.",
                error_code="destination_room_not_adjacent",
                errors=["destination_room_not_adjacent"],
            )

        previous_location = current_location
        npc["location"] = action.destination_room_id
        result = self._result(
            success=True,
            changed=True,
            action=WorldActionType.MOVE_NPC,
            summary=f"Moved NPC '{action.npc_id}' from '{previous_location}' to '{action.destination_room_id}'.",
            state_delta={
                "npcs": {
                    action.npc_id: {
                        "location": {
                            "from": previous_location,
                            "to": action.destination_room_id,
                        }
                    }
                }
            },
        )
        return candidate, result

    def _execute_set_npc_status(
        self,
        candidate: dict[str, Any],
        action: SetNpcStatusWorldAction,
        original_state: dict[str, Any],
    ) -> tuple[dict[str, Any], WorldActionResult]:
        npcs = candidate.get("npcs")
        if not isinstance(npcs, dict):
            return original_state, self._result(
                success=False,
                action=WorldActionType.SET_NPC_STATUS,
                summary="NPC state is malformed.",
                error_code="malformed_npcs_state",
                errors=["malformed_npcs_state"],
            )

        npc = npcs.get(action.npc_id)
        if not isinstance(npc, dict):
            return original_state, self._result(
                success=False,
                action=WorldActionType.SET_NPC_STATUS,
                summary=f"NPC '{action.npc_id}' does not exist.",
                error_code="npc_not_found",
                errors=["npc_not_found"],
            )

        current_status = npc.get("status")
        if not isinstance(current_status, str) or current_status not in {"active", "absent"}:
            return original_state, self._result(
                success=False,
                action=WorldActionType.SET_NPC_STATUS,
                summary=f"NPC '{action.npc_id}' has an invalid status value.",
                error_code="invalid_npc_status",
                errors=["invalid_npc_status"],
            )

        if current_status == action.status:
            return candidate, self._result(
                success=True,
                changed=False,
                action=WorldActionType.SET_NPC_STATUS,
                summary=f"NPC '{action.npc_id}' is already {action.status}.",
                state_delta={},
            )

        previous_status = current_status
        npc["status"] = action.status
        result = self._result(
            success=True,
            changed=True,
            action=WorldActionType.SET_NPC_STATUS,
            summary=f"Set NPC '{action.npc_id}' status to '{action.status}'.",
            state_delta={
                "npcs": {
                    action.npc_id: {
                        "status": {
                            "from": previous_status,
                            "to": action.status,
                        }
                    }
                }
            },
        )
        return candidate, result

    def _execute_advance_clock(
        self,
        candidate: dict[str, Any],
        action: AdvanceClockWorldAction,
        original_state: dict[str, Any],
    ) -> tuple[dict[str, Any], WorldActionResult]:
        clock = candidate.get("clock")
        if not isinstance(clock, dict):
            return original_state, self._result(
                success=False,
                action=WorldActionType.ADVANCE_CLOCK,
                summary="Clock state is malformed.",
                error_code="invalid_clock_state",
                errors=["invalid_clock_state"],
            )

        current_tick = clock.get("tick")
        if type(current_tick) is not int:
            return original_state, self._result(
                success=False,
                action=WorldActionType.ADVANCE_CLOCK,
                summary="Clock tick value is not a valid integer.",
                error_code="invalid_clock_state",
                errors=["invalid_clock_state"],
            )

        if not 1 <= action.ticks <= 10:
            return original_state, self._result(
                success=False,
                action=WorldActionType.ADVANCE_CLOCK,
                summary="Clock ticks must be in the inclusive range 1 through 10.",
                error_code="ticks_out_of_range",
                errors=["ticks_out_of_range"],
            )

        previous_tick = current_tick
        next_tick = previous_tick + action.ticks
        clock["tick"] = next_tick

        result = self._result(
            success=True,
            changed=True,
            action=WorldActionType.ADVANCE_CLOCK,
            summary=f"Advanced clock by {action.ticks} tick(s).",
            state_delta={
                "clock": {
                    "tick": {
                        "from": previous_tick,
                        "to": next_tick,
                    }
                }
            },
        )
        return candidate, result

    def _execute_record_fact(
        self,
        candidate: dict[str, Any],
        action: RecordFactWorldAction,
        original_state: dict[str, Any],
    ) -> tuple[dict[str, Any], WorldActionResult]:
        facts = candidate.get("facts")
        if not isinstance(facts, list):
            return original_state, self._result(
                success=False,
                action=WorldActionType.RECORD_FACT,
                summary="Facts state is malformed.",
                error_code="malformed_facts_state",
                errors=["malformed_facts_state"],
            )

        normalized_fact = action.fact.strip()
        if not normalized_fact:
            return original_state, self._result(
                success=False,
                action=WorldActionType.RECORD_FACT,
                summary="Fact text is empty after trimming.",
                error_code="invalid_fact",
                errors=["invalid_fact"],
            )
        if len(normalized_fact) > 512:
            return original_state, self._result(
                success=False,
                action=WorldActionType.RECORD_FACT,
                summary="Fact text exceeds 512 characters after trimming.",
                error_code="invalid_fact",
                errors=["invalid_fact"],
            )
        if any(not isinstance(entry, str) for entry in facts):
            return original_state, self._result(
                success=False,
                action=WorldActionType.RECORD_FACT,
                summary="Facts state contains non-string entries.",
                error_code="malformed_facts_state",
                errors=["malformed_facts_state"],
            )

        if normalized_fact in facts:
            return candidate, self._result(
                success=True,
                changed=False,
                action=WorldActionType.RECORD_FACT,
                summary=f"Fact '{normalized_fact}' is already recorded.",
                state_delta={},
            )

        if len(set(facts)) >= 256:
            return original_state, self._result(
                success=False,
                action=WorldActionType.RECORD_FACT,
                summary="The fact list has reached the 256 unique-fact limit.",
                error_code="facts_limit_reached",
                errors=["facts_limit_reached"],
            )

        facts.append(normalized_fact)
        result = self._result(
            success=True,
            changed=True,
            action=WorldActionType.RECORD_FACT,
            summary=f"Recorded fact '{normalized_fact}'.",
            state_delta={
                "facts": {
                    "added": [normalized_fact],
                }
            },
        )
        return candidate, result

    def _coerce_action(self, action: WorldAction | dict[str, Any]) -> WorldAction | None:
        if isinstance(action, dict):
            action_type = action.get("action") or action.get("type")
            if action_type == WorldActionType.MOVE_NPC:
                return MoveNpcWorldAction.model_validate(action)
            if action_type == WorldActionType.SET_NPC_STATUS:
                return SetNpcStatusWorldAction.model_validate(action)
            if action_type == WorldActionType.ADVANCE_CLOCK:
                return AdvanceClockWorldAction.model_validate(action)
            if action_type == WorldActionType.RECORD_FACT:
                return RecordFactWorldAction.model_validate(action)
            if isinstance(action_type, str):
                lower = action_type.lower()
                if lower == "move_npc":
                    return MoveNpcWorldAction.model_validate({**action, "action": WorldActionType.MOVE_NPC})
                if lower == "set_npc_status":
                    return SetNpcStatusWorldAction.model_validate({**action, "action": WorldActionType.SET_NPC_STATUS})
                if lower == "advance_clock":
                    return AdvanceClockWorldAction.model_validate({**action, "action": WorldActionType.ADVANCE_CLOCK})
                if lower == "record_fact":
                    return RecordFactWorldAction.model_validate({**action, "action": WorldActionType.RECORD_FACT})
            return None
        return action

    def _result(
        self,
        *,
        success: bool,
        action: str | WorldActionType,
        summary: str,
        error_code: str | None = None,
        errors: list[str] | None = None,
        changed: bool = False,
        state_delta: dict[str, Any] | None = None,
    ) -> WorldActionResult:
        return WorldActionResult(
            success=success,
            changed=changed,
            action=str(action),
            summary=summary,
            state_delta=state_delta or {},
            error_code=error_code,
            errors=errors or ([] if success else [error_code] if error_code is not None else []),
        )
