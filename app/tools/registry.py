from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, Protocol


ToolRegistryMode = Literal["local", "mcp", "hybrid"]


class MCPToolClient(Protocol):
    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        ...


class RegistryTransportError(RuntimeError):
    pass


class ToolRegistry:
    """Callable registry with pluggable local/MCP dispatch."""

    def __init__(
        self,
        *,
        mode: ToolRegistryMode = "local",
        mcp_client: MCPToolClient | None = None,
    ) -> None:
        self._tools: dict[str, Callable[..., Any]] = {}
        self._mcp_tools: dict[str, str] = {}
        self._mode: ToolRegistryMode = mode
        self._mcp_client = mcp_client

    def register(self, name: str, tool: Callable[..., Any]) -> None:
        self._tools[name] = tool

    def register_mcp(self, name: str, mcp_name: str | None = None) -> None:
        self._mcp_tools[name] = mcp_name or name

    def get(self, name: str) -> Callable[..., Any] | None:
        return self._tools.get(name)

    def execute(self, name: str, *args: Any, **kwargs: Any) -> Any:
        local_tool = self.get(name)
        prefers_mcp = self._mode in {"mcp", "hybrid"} and name in self._mcp_tools
        can_try_mcp = self._mcp_client is not None and name in self._mcp_tools

        if prefers_mcp and can_try_mcp:
            try:
                return self._execute_mcp(name, *args, **kwargs)
            except RegistryTransportError:
                if local_tool is None:
                    raise

        if local_tool is not None:
            return local_tool(*args, **kwargs)

        if can_try_mcp:
            return self._execute_mcp(name, *args, **kwargs)

        raise KeyError(f"Tool not registered: {name}")

    def _execute_mcp(self, name: str, *args: Any, **kwargs: Any) -> Any:
        if self._mcp_client is None:
            raise RegistryTransportError("No MCP client configured.")

        mcp_name = self._mcp_tools.get(name)
        if not mcp_name:
            raise KeyError(f"MCP tool not registered: {name}")

        try:
            response = self._mcp_client.call_tool(
                mcp_name,
                {
                    "args": list(args),
                    "kwargs": kwargs,
                },
            )
        except Exception as exc:  # noqa: BLE001
            raise RegistryTransportError(f"MCP call failed for tool: {mcp_name}") from exc

        if not isinstance(response, dict):
            return response

        if "structured_content" in response or "content" in response:
            return response
        if "return" in response:
            return response["return"]
        return response
