from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.schemas.chat import ParsedAction
from app.services.tool_executor import ToolExecutor
from app.tools import mcp_client as mcp_client_module
from app.tools.mcp_client import DisabledMCPClient, build_mcp_client
from app.tools.registry import ToolRegistry


class FakeMCPClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        if name == "advance_time":
            state = arguments["args"][0]
            ticks = int(arguments["args"][1])
            state.setdefault("clock", {})["tick"] = int(state.get("clock", {}).get("tick", 0)) + ticks
            return {"return": None}
        if name == "double":
            value = arguments["args"][0]
            return {"return": value * 3}
        return {"return": None}


class SDKStyleMCPClient:
    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        state = arguments["args"][0]

        if name == "advance_time":
            ticks = int(arguments["args"][1])
            next_state = {
                **state,
                "clock": {
                    "tick": int(state.get("clock", {}).get("tick", 0)) + ticks,
                },
            }
            return {
                "structured_content": {"state": next_state},
                "content": "advanced",
                "return": {"state": next_state},
            }

        return {"structured_content": {"state": state}, "content": "ok", "return": {"state": state}}


class FailingMCPClient:
    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:  # noqa: ARG002
        raise RuntimeError(f"failed: {name}")


def test_registry_executes_local_tool_in_local_mode() -> None:
    registry = ToolRegistry(mode="local")
    registry.register("double", lambda value: value * 2)

    result = registry.execute("double", 21)

    assert result == 42


def test_registry_executes_mcp_tool_when_mode_is_mcp() -> None:
    client = FakeMCPClient()
    registry = ToolRegistry(mode="mcp", mcp_client=client)
    registry.register_mcp("advance_clock", "advance_time")

    result = registry.execute("advance_clock", {"clock": {"tick": 2}}, 3)

    assert result is None
    assert client.calls[0][0] == "advance_time"


def test_registry_falls_back_to_local_when_mcp_not_registered() -> None:
    client = FakeMCPClient()
    registry = ToolRegistry(mode="mcp", mcp_client=client)
    registry.register("double", lambda value: value * 2)

    result = registry.execute("double", 21)

    assert result == 42
    assert client.calls == []


def test_registry_hybrid_prefers_mcp_when_mapping_exists() -> None:
    client = FakeMCPClient()
    registry = ToolRegistry(mode="hybrid", mcp_client=client)
    registry.register("double", lambda value: value * 2)
    registry.register_mcp("double", "double")

    result = registry.execute("double", 7)

    assert result == 21
    assert client.calls[0][0] == "double"


def test_registry_hybrid_falls_back_to_local_on_mcp_error() -> None:
    registry = ToolRegistry(mode="hybrid", mcp_client=FailingMCPClient())
    registry.register("double", lambda value: value * 2)
    registry.register_mcp("double", "double")

    result = registry.execute("double", 7)

    assert result == 14


def test_tool_executor_can_use_mcp_registry_without_orchestrator_changes() -> None:
    client = FakeMCPClient()
    registry = ToolRegistry(mode="mcp", mcp_client=client)

    def local_unsupported(*args: Any, **kwargs: Any) -> None:  # noqa: ARG001
        raise AssertionError("Local path should not be used when MCP mapping exists")

    registry.register("advance_clock", local_unsupported)
    registry.register_mcp("advance_clock", "advance_time")

    executor = ToolExecutor(registry=registry)
    parsed_action = ParsedAction(
        raw_text="wait",
        action="advance_clock",
        parameters={"amount": 3},
        parse_status="ok",
    )

    state, result = executor.execute(parsed_action=parsed_action, campaign_state='{"clock": {"tick": 1}}')

    assert result.success is True
    assert result.applied_tools == ["advance_clock"]
    assert state["clock"]["tick"] == 4
    assert client.calls[0][0] == "advance_time"


def test_tool_executor_applies_structured_state_from_remote_mcp_result() -> None:
    registry = ToolRegistry(mode="mcp", mcp_client=SDKStyleMCPClient())
    registry.register_mcp("advance_clock", "advance_time")
    executor = ToolExecutor(registry=registry)

    parsed_action = ParsedAction(
        raw_text="wait",
        action="advance_clock",
        parameters={"amount": 2},
        parse_status="ok",
    )

    state, result = executor.execute(parsed_action=parsed_action, campaign_state='{"clock": {"tick": 1}}')

    assert result.success is True
    assert state["clock"]["tick"] == 3


def test_build_mcp_client_uses_sdk_client_when_http_configured(monkeypatch) -> None:
    original_transport = settings.MCP_TRANSPORT
    original_url = settings.MCP_SERVER_URL
    original_command = settings.MCP_SERVER_COMMAND
    try:
        settings.MCP_TRANSPORT = "streamable_http"
        settings.MCP_SERVER_URL = "http://localhost:8000/mcp"
        settings.MCP_SERVER_COMMAND = None
        sentinel = object()
        monkeypatch.setattr(mcp_client_module, "SDKMCPClient", lambda: sentinel)

        client = build_mcp_client()

        assert client is sentinel
    finally:
        settings.MCP_TRANSPORT = original_transport
        settings.MCP_SERVER_URL = original_url
        settings.MCP_SERVER_COMMAND = original_command


def test_build_mcp_client_returns_disabled_without_transport_config() -> None:
    original_transport = settings.MCP_TRANSPORT
    original_url = settings.MCP_SERVER_URL
    original_command = settings.MCP_SERVER_COMMAND
    try:
        settings.MCP_TRANSPORT = "streamable_http"
        settings.MCP_SERVER_URL = None
        settings.MCP_SERVER_COMMAND = None

        client = build_mcp_client()

        assert isinstance(client, DisabledMCPClient)
    finally:
        settings.MCP_TRANSPORT = original_transport
        settings.MCP_SERVER_URL = original_url
        settings.MCP_SERVER_COMMAND = original_command
