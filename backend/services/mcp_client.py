"""Thin async wrapper over the official MCP SDK transports.

We own: per-call sessions, header plumbing, per-server TLS policy, timeouts, and
error normalization. The SDK owns: JSON-RPC framing, the handshake, and SSE
parsing.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import anyio
import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from backend.utils.errors import flatten_error


class MCPError(RuntimeError):
    """Normalized transport/dispatch failure for an MCP operation."""


@dataclass
class DiscoveredTool:
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCallOutcome:
    text: str
    is_error: bool = False


def _client_factory(verify_ssl: bool):
    def factory(headers=None, timeout=None, auth=None):
        kwargs: dict[str, Any] = {"verify": verify_ssl}
        if timeout is not None:
            kwargs["timeout"] = timeout
        if headers is not None:
            kwargs["headers"] = headers
        if auth is not None:  # pragma: no cover - unused locally
            kwargs["auth"] = auth
        return httpx.AsyncClient(**kwargs)

    return factory


@asynccontextmanager
async def open_session(
    *,
    url: str | None = None,
    headers: dict[str, str] | None = None,
    timeout_ms: int = 30_000,
    verify_ssl: bool = True,
    transport: str = "http",
    command: str | None = None,
    args: list[str] | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
):
    """Open, initialize and yield one MCP client session."""
    headers = headers or {}
    read_timeout = max(5.0, timeout_ms / 1000.0)
    factory = _client_factory(verify_ssl)
    if not verify_ssl:
        raise MCPError("TLS verification cannot be disabled")
    try:
        with anyio.fail_after(timeout_ms / 1000):
            async with _open_transport(
                url=url,
                headers=headers,
                transport=transport,
                command=command,
                args=args,
                cwd=cwd,
                env=env,
                read_timeout=read_timeout,
                factory=factory,
            ) as session:
                yield session
    except MCPError:
        raise
    except Exception as exc:
        raise MCPError(_describe(exc)) from exc


@asynccontextmanager
async def _open_transport(
    *, url, headers, transport, command, args, cwd, env, read_timeout, factory
):
    try:
        if transport == "stdio":
            if not command:
                raise MCPError("stdio server has no command configured")
            params = StdioServerParameters(command=command, args=args or [], cwd=cwd, env=env)
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        elif transport == "sse":
            async with sse_client(
                url or "",
                headers=headers,
                timeout=read_timeout,
                sse_read_timeout=read_timeout,
                httpx_client_factory=factory,
            ) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        else:
            async with streamablehttp_client(
                url or "",
                headers=headers,
                timeout=read_timeout,
                sse_read_timeout=read_timeout,
                httpx_client_factory=factory,
            ) as (read, write, _get_session_id):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
    except MCPError:
        raise
    except Exception as exc:  # CancelledError is BaseException and passes through
        raise MCPError(_describe(exc)) from exc


def _describe(exc: BaseException) -> str:
    flat = flatten_error(exc)
    return f"{type(flat).__name__}: {flat}"


async def list_tools(
    *,
    url: str | None = None,
    headers: dict[str, str],
    timeout_ms: int,
    verify_ssl: bool,
    transport: str,
    command: str | None = None,
    args: list[str] | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> list[DiscoveredTool]:
    async with open_session(
        url=url,
        headers=headers,
        timeout_ms=timeout_ms,
        verify_ssl=verify_ssl,
        transport=transport,
        command=command,
        args=args,
        cwd=cwd,
        env=env,
    ) as session:
        tools: list[DiscoveredTool] = []
        cursor = None
        seen = set()
        for _ in range(100):
            result = await session.list_tools(cursor=cursor)
            for tool in result.tools:
                if tool.name in seen:
                    raise MCPError("duplicate tool name in discovery")
                seen.add(tool.name)
                tools.append(
                    DiscoveredTool(
                        name=tool.name,
                        description=tool.description or "",
                        input_schema=tool.inputSchema,
                    )
                )
            cursor = result.nextCursor
            if not cursor:
                break
        else:
            raise MCPError("tool discovery exceeded 100 pages")
        return tools


async def call_tool(
    *,
    url: str | None = None,
    headers: dict[str, str],
    timeout_ms: int,
    verify_ssl: bool,
    transport: str,
    tool_name: str,
    arguments: dict[str, Any],
    command: str | None = None,
    args: list[str] | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> ToolCallOutcome:
    async with open_session(
        url=url,
        headers=headers,
        timeout_ms=timeout_ms,
        verify_ssl=verify_ssl,
        transport=transport,
        command=command,
        args=args,
        cwd=cwd,
        env=env,
    ) as session:
        result = await session.call_tool(tool_name, arguments)
        return ToolCallOutcome(text=_content_to_text(result), is_error=bool(result.isError))


def _content_to_text(result: Any) -> str:
    parts: list[str] = []
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if text is None:
            dumper = getattr(item, "model_dump_json", None)
            text = dumper() if callable(dumper) else str(item)
        parts.append(str(text))
    if not parts:
        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            import json

            return json.dumps(structured)
    return "\n".join(parts)
