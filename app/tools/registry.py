from __future__ import annotations

from collections.abc import Callable
from typing import Any


class ToolRegistry:
    """Simple callable registry for deterministic tool dispatch."""

    def __init__(self) -> None:
        self._tools: dict[str, Callable[..., Any]] = {}

    def register(self, name: str, tool: Callable[..., Any]) -> None:
        self._tools[name] = tool

    def get(self, name: str) -> Callable[..., Any] | None:
        return self._tools.get(name)

    def execute(self, name: str, *args: Any, **kwargs: Any) -> Any:
        tool = self.get(name)
        if tool is None:
            raise KeyError(f"Tool not registered: {name}")
        return tool(*args, **kwargs)
