from __future__ import annotations

from typing import Any

import pytest

from app.schemas.chat import ActionType, ParsedAction
from app.services.tool_executor import ToolExecutor
from app.tools.registry import ToolRegistry


class ParityMCPClient:
    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        args = arguments.get("args", [])
        state = args[0]

        if name == "get_room":
            room_id = args[1]
            state.setdefault("player", {})["location"] = room_id
            return {"return": None}

        if name == "create_npc":
            npc_id = args[1]
            room_id = args[2]
            state.setdefault("npcs", {})[npc_id] = {"room": room_id}
            return {"return": None}

        if name == "advance_time":
            ticks = int(args[1])
            clock = state.setdefault("clock", {})
            clock["tick"] = int(clock.get("tick", 0)) + ticks
            return {"return": None}

        if name == "search_lore":
            fact = args[1]
            state.setdefault("facts", []).append(fact)
            return {"return": None}

        raise RuntimeError(f"unknown mcp tool: {name}")


class FailingMCPClient:
    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:  # noqa: ARG002
        raise RuntimeError(f"cannot reach mcp tool {name}")


def _build_local_executor() -> ToolExecutor:
    registry = ToolRegistry(mode="local")
    executor = ToolExecutor(registry=registry)
    registry.register("move_player", executor.move_player)
    registry.register("add_inventory", executor.add_inventory)
    registry.register("remove_inventory", executor.remove_inventory)
    registry.register("spawn_npc", executor.spawn_npc)
    registry.register("advance_clock", executor.advance_clock)
    registry.register("record_fact", executor.record_fact)
    return executor


def _build_hybrid_executor() -> ToolExecutor:
    registry = ToolRegistry(mode="hybrid", mcp_client=ParityMCPClient())
    executor = ToolExecutor(registry=registry)
    registry.register("move_player", executor.move_player)
    registry.register("add_inventory", executor.add_inventory)
    registry.register("remove_inventory", executor.remove_inventory)
    registry.register("spawn_npc", executor.spawn_npc)
    registry.register("advance_clock", executor.advance_clock)
    registry.register("record_fact", executor.record_fact)
    registry.register_mcp("move_player", "get_room")
    registry.register_mcp("spawn_npc", "create_npc")
    registry.register_mcp("advance_clock", "advance_time")
    registry.register_mcp("record_fact", "search_lore")
    return executor


@pytest.mark.parametrize(
    "parsed_action,campaign_state",
    [
        (
            ParsedAction(raw_text="go roof", action=ActionType.MOVE, target="roof", parse_status="ok"),
            "No campaign state yet.",
        ),
        (
            ParsedAction(raw_text="take key", action=ActionType.TAKE, target="rusty_key", parse_status="ok"),
            "No campaign state yet.",
        ),
        (
            ParsedAction(raw_text="drop key", action=ActionType.DROP, target="rusty_key", parse_status="ok"),
            '{"player": {"location": "entry_hall", "inventory": ["rusty_key"]}, "npcs": {}, "clock": {"tick": 0}, "facts": []}',
        ),
        (
            ParsedAction(raw_text="wait", action=ActionType.WAIT, parameters={"amount": 2}, parse_status="ok"),
            "No campaign state yet.",
        ),
    ],
)
def test_tool_executor_transport_parity(parsed_action: ParsedAction, campaign_state: str) -> None:
    local_executor = _build_local_executor()
    hybrid_executor = _build_hybrid_executor()

    local_state, local_result = local_executor.execute(parsed_action=parsed_action, campaign_state=campaign_state)
    hybrid_state, hybrid_result = hybrid_executor.execute(parsed_action=parsed_action, campaign_state=campaign_state)

    assert local_result.success == hybrid_result.success
    assert local_result.applied_tools == hybrid_result.applied_tools
    assert local_result.summary == hybrid_result.summary
    assert local_state == hybrid_state


def test_tool_executor_returns_structured_dispatch_errors() -> None:
    registry = ToolRegistry(mode="mcp", mcp_client=FailingMCPClient())
    registry.register_mcp("advance_clock", "advance_time")
    executor = ToolExecutor(registry=registry)

    parsed_action = ParsedAction(
        raw_text="wait",
        action=ActionType.WAIT,
        parameters={"amount": 1},
        parse_status="ok",
    )

    _, result = executor.execute(parsed_action=parsed_action, campaign_state="No campaign state yet.")

    assert result.success is False
    assert result.summary == "Tool dispatch failed for advance_clock."
    assert result.errors[0] == "tool_dispatch_failed"
    assert result.errors[1] == "tool:advance_clock"
    assert result.errors[2].startswith("reason:MCP call failed for tool: advance_time")
