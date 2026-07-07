from __future__ import annotations

import copy
import json
from typing import Any

from app.core.config import settings
from app.schemas.chat import ParsedAction, ToolExecutionResult
from app.tools.mcp_client import build_mcp_client
from app.tools.registry import RegistryTransportError, ToolRegistry

DEFAULT_CAMPAIGN_STATE: dict[str, Any] = {
    "player": {
        "location": "entry_hall",
        "inventory": [],
    },
    "npcs": {},
    "clock": {
        "tick": 0,
    },
    "facts": [],
}


class ToolExecutor:
    def __init__(self, registry: ToolRegistry | None = None) -> None:
        self.registry = registry or self._build_registry()

    def _build_registry(self) -> ToolRegistry:
        mcp_client = build_mcp_client() if settings.TOOL_REGISTRY_TRANSPORT in {"mcp", "hybrid"} else None
        registry = ToolRegistry(mode=settings.TOOL_REGISTRY_TRANSPORT, mcp_client=mcp_client)
        registry.register("move_player", self.move_player)
        registry.register("add_inventory", self.add_inventory)
        registry.register("remove_inventory", self.remove_inventory)
        registry.register("spawn_npc", self.spawn_npc)
        registry.register("advance_clock", self.advance_clock)
        registry.register("record_fact", self.record_fact)
        registry.register_mcp("spawn_npc", "create_npc")
        registry.register_mcp("advance_clock", "advance_time")
        registry.register_mcp("move_player", "get_room")
        registry.register_mcp("record_fact", "search_lore")
        return registry

    def execute(self, *, parsed_action: ParsedAction, campaign_state: str) -> tuple[dict[str, Any], ToolExecutionResult]:
        state = self._state_from_text(campaign_state)
        previous_state = copy.deepcopy(state)

        action = parsed_action.action.lower().strip()
        target = parsed_action.target

        result = ToolExecutionResult(
            success=False,
            summary="No executable tool matched this action.",
        )

        if parsed_action.parse_status != "ok":
            result.summary = "Action parse was not confident enough for tool execution."
            return state, result

        if action in {"move", "go", "walk", "run", "enter", "climb"}:
            room = target or "unknown_location"
            dispatch_error = self._dispatch_tool("move_player", state, room)
            if dispatch_error is not None:
                return state, dispatch_error
            result = ToolExecutionResult(
                success=True,
                applied_tools=["move_player"],
                summary=f"Player moved to {room}.",
            )
            if parsed_action.stealth:
                dispatch_error = self._dispatch_tool("record_fact", state, f"player attempted stealth movement to {room}")
                if dispatch_error is not None:
                    return state, dispatch_error
                result.applied_tools.append("record_fact")
                result.summary += " Stealth movement was recorded."

        elif action in {"take", "grab", "collect"}:
            item = target or parsed_action.parameters.get("item") or "unknown_item"
            dispatch_error = self._dispatch_tool("add_inventory", state, str(item))
            if dispatch_error is not None:
                return state, dispatch_error
            result = ToolExecutionResult(
                success=True,
                applied_tools=["add_inventory"],
                summary=f"Added {item} to inventory.",
            )

        elif action in {"drop", "remove", "discard"}:
            item = target or parsed_action.parameters.get("item") or "unknown_item"
            removed, dispatch_error = self._dispatch_tool_with_value("remove_inventory", state, str(item))
            if dispatch_error is not None:
                return state, dispatch_error
            if removed:
                result = ToolExecutionResult(
                    success=True,
                    applied_tools=["remove_inventory"],
                    summary=f"Removed {item} from inventory.",
                )
            else:
                result = ToolExecutionResult(
                    success=False,
                    summary=f"Item {item} was not in inventory.",
                    errors=["item_not_found"],
                )

        elif action == "spawn_npc":
            npc_id = target or parsed_action.parameters.get("npc") or "mysterious_figure"
            room = str(parsed_action.parameters.get("room") or state["player"]["location"])
            dispatch_error = self._dispatch_tool("spawn_npc", state, str(npc_id), room)
            if dispatch_error is not None:
                return state, dispatch_error
            result = ToolExecutionResult(
                success=True,
                applied_tools=["spawn_npc"],
                summary=f"Spawned {npc_id} in {room}.",
            )

        elif action == "advance_clock":
            amount = parsed_action.parameters.get("amount", 1)
            try:
                ticks = max(1, int(amount))
            except (TypeError, ValueError):
                ticks = 1
            dispatch_error = self._dispatch_tool("advance_clock", state, ticks)
            if dispatch_error is not None:
                return state, dispatch_error
            result = ToolExecutionResult(
                success=True,
                applied_tools=["advance_clock"],
                summary=f"Advanced clock by {ticks} tick(s).",
            )

        elif action == "record_fact":
            fact = str(parsed_action.parameters.get("fact") or parsed_action.raw_text)
            dispatch_error = self._dispatch_tool("record_fact", state, fact)
            if dispatch_error is not None:
                return state, dispatch_error
            result = ToolExecutionResult(
                success=True,
                applied_tools=["record_fact"],
                summary="Recorded a campaign fact.",
            )

        if state != previous_state:
            result.state_delta = self._compute_state_delta(previous_state, state)
        return state, result

    def move_player(self, state: dict[str, Any], room_id: str) -> None:
        state.setdefault("player", {}).update({"location": room_id})

    def add_inventory(self, state: dict[str, Any], item: str) -> None:
        inventory = state.setdefault("player", {}).setdefault("inventory", [])
        if item not in inventory:
            inventory.append(item)

    def remove_inventory(self, state: dict[str, Any], item: str) -> bool:
        inventory = state.setdefault("player", {}).setdefault("inventory", [])
        if item not in inventory:
            return False
        inventory.remove(item)
        return True

    def spawn_npc(self, state: dict[str, Any], npc_id: str, room_id: str) -> None:
        npcs = state.setdefault("npcs", {})
        npcs[npc_id] = {
            "room": room_id,
        }

    def advance_clock(self, state: dict[str, Any], amount: int) -> None:
        clock = state.setdefault("clock", {})
        clock["tick"] = int(clock.get("tick", 0)) + amount

    def record_fact(self, state: dict[str, Any], fact: str) -> None:
        facts = state.setdefault("facts", [])
        facts.append(fact)

    def _state_from_text(self, campaign_state: str) -> dict[str, Any]:
        if not campaign_state or campaign_state == "No campaign state yet.":
            return copy.deepcopy(DEFAULT_CAMPAIGN_STATE)
        try:
            value = json.loads(campaign_state)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
        return copy.deepcopy(DEFAULT_CAMPAIGN_STATE)

    def _compute_state_delta(self, before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
        delta: dict[str, Any] = {}
        for key in set(before.keys()) | set(after.keys()):
            if before.get(key) != after.get(key):
                delta[key] = after.get(key)
        return delta

    def _dispatch_tool(self, tool_name: str, *args: Any, **kwargs: Any) -> ToolExecutionResult | None:
        try:
            result = self.registry.execute(tool_name, *args, **kwargs)
            self._apply_remote_state_result(args, result)
            return None
        except (KeyError, RegistryTransportError) as exc:
            return self._build_dispatch_error(tool_name, exc)

    def _dispatch_tool_with_value(
        self,
        tool_name: str,
        *args: Any,
        **kwargs: Any,
    ) -> tuple[Any, ToolExecutionResult | None]:
        try:
            result = self.registry.execute(tool_name, *args, **kwargs)
            self._apply_remote_state_result(args, result)
            return result, None
        except (KeyError, RegistryTransportError) as exc:
            return None, self._build_dispatch_error(tool_name, exc)

    def _build_dispatch_error(self, tool_name: str, exc: Exception) -> ToolExecutionResult:
        return ToolExecutionResult(
            success=False,
            summary=f"Tool dispatch failed for {tool_name}.",
            errors=[
                "tool_dispatch_failed",
                f"tool:{tool_name}",
                f"reason:{exc}",
            ],
        )

    def _apply_remote_state_result(self, args: tuple[Any, ...], result: Any) -> None:
        if not args:
            return
        state = args[0]
        if not isinstance(state, dict):
            return
        if not isinstance(result, dict):
            return

        structured_content = result.get("structured_content")
        if not isinstance(structured_content, dict):
            return

        next_state = structured_content.get("state")
        if not isinstance(next_state, dict):
            return

        state.clear()
        state.update(next_state)
