from __future__ import annotations

import asyncio
import threading
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from app.core.config import settings
from app.tools.registry import MCPToolClient, RegistryTransportError


class SDKMCPClient(MCPToolClient):
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._session_future = asyncio.run_coroutine_threadsafe(self._connect(), self._loop)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        try:
            session = self._session_future.result(timeout=self._timeout_seconds())
            result = asyncio.run_coroutine_threadsafe(
                session.call_tool(
                    name,
                    arguments,
                    read_timeout_seconds=timedelta(milliseconds=settings.MCP_REQUEST_TIMEOUT_MS),
                ),
                self._loop,
            ).result(timeout=self._timeout_seconds())
        except Exception as exc:  # noqa: BLE001
            raise RegistryTransportError(f"MCP SDK call failed for tool: {name}") from exc

        if getattr(result, "isError", False):
            raise RegistryTransportError(self._error_message(name, result))

        structured = getattr(result, "structuredContent", None)
        content_text = self._content_text(result)
        return {
            "structured_content": structured,
            "content": content_text,
            "return": structured if structured is not None else content_text,
        }

    def close(self) -> None:
        try:
            asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop).result(timeout=self._timeout_seconds())
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=1)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _connect(self) -> ClientSession:
        self._exit_stack = AsyncExitStack()
        timeout = timedelta(milliseconds=settings.MCP_REQUEST_TIMEOUT_MS)
        is_url_transport = settings.MCP_TRANSPORT in {"sse", "streamable_http"}

        if settings.MCP_TRANSPORT == "stdio" and not settings.MCP_SERVER_COMMAND:
            raise RegistryTransportError("MCP_SERVER_COMMAND is required for stdio transport.")

        if is_url_transport and not settings.MCP_SERVER_URL:
            raise RegistryTransportError(
                f"MCP_SERVER_URL is required for {settings.MCP_TRANSPORT} transport."
            )

        if settings.MCP_TRANSPORT == "stdio":
            command = settings.MCP_SERVER_COMMAND
            if command is None:
                raise RegistryTransportError("MCP_SERVER_COMMAND is required for stdio transport.")
            read_stream, write_stream = await self._exit_stack.enter_async_context(
                stdio_client(
                    StdioServerParameters(
                        command=command,
                        args=settings.MCP_SERVER_ARGS,
                        cwd=settings.MCP_SERVER_CWD,
                    )
                )
            )
        elif settings.MCP_TRANSPORT == "sse":
            server_url = settings.MCP_SERVER_URL
            if server_url is None:
                raise RegistryTransportError("MCP_SERVER_URL is required for sse transport.")
            read_stream, write_stream = await self._exit_stack.enter_async_context(
                sse_client(
                    server_url,
                    timeout=float(timeout.total_seconds()),
                    sse_read_timeout=float(max(timeout.total_seconds(), 5.0)),
                )
            )
        else:
            server_url = settings.MCP_SERVER_URL
            if server_url is None:
                raise RegistryTransportError("MCP_SERVER_URL is required for streamable_http transport.")
            http_client = await self._exit_stack.enter_async_context(
                httpx.AsyncClient(
                    timeout=httpx.Timeout(
                        timeout.total_seconds(),
                        read=max(timeout.total_seconds(), 5.0),
                    )
                )
            )
            read_stream, write_stream, _ = await self._exit_stack.enter_async_context(
                streamable_http_client(
                    server_url,
                    http_client=http_client,
                )
            )

        session = await self._exit_stack.enter_async_context(
            ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=timeout,
            )
        )
        await session.initialize()
        return session

    async def _shutdown(self) -> None:
        exit_stack = getattr(self, "_exit_stack", None)
        if exit_stack is not None:
            await exit_stack.aclose()

    def _content_text(self, result: Any) -> str:
        content = getattr(result, "content", []) or []
        parts: list[str] = []
        for item in content:
            text = getattr(item, "text", None)
            if isinstance(text, str) and text:
                parts.append(text)
        return "\n".join(parts)

    def _error_message(self, name: str, result: Any) -> str:
        content_text = self._content_text(result)
        if content_text:
            return content_text
        return f"MCP tool reported an error: {name}"

    def _timeout_seconds(self) -> float:
        return max(settings.MCP_REQUEST_TIMEOUT_MS / 1000, 1.0)


class DisabledMCPClient(MCPToolClient):
    """Disabled MCP transport used when no concrete MCP configuration exists."""

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:  # noqa: ARG002
        raise RegistryTransportError(
            "MCP transport is not configured. "
            "Set MCP transport settings to enable the MCP SDK client."
        )


def build_mcp_client() -> MCPToolClient:
    if settings.MCP_TRANSPORT == "stdio" and settings.MCP_SERVER_COMMAND:
        return SDKMCPClient()
    if settings.MCP_TRANSPORT in {"streamable_http", "sse"} and settings.MCP_SERVER_URL:
        return SDKMCPClient()
    return DisabledMCPClient()
