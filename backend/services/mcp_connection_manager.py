"""Transport dispatch for MCP servers.

Supports the in-process ``local`` transport and remote ``http`` (Streamable HTTP)
/ ``sse`` (legacy) transports. ``stdio`` is operator-managed and opt-in.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from backend.repositories.mcp_repository import MCPServerRepository
from backend.services.mcp_client import MCPError, ToolCallOutcome
from backend.services.mcp_client import call_tool as mcp_call_tool
from backend.services.mcp_client import list_tools as mcp_list_tools
from backend.services.sandbox import dispatch as sandbox_dispatch
from backend.services.sandbox import tools_for as sandbox_tools_for

__all__ = ["MCPConnectionManager", "MCPError", "ToolCallOutcome"]


class MCPConnectionManager:
    def __init__(self, settings, secret_resolver=None) -> None:
        self.settings = settings
        self.secret_resolver = secret_resolver

    # -- headers --------------------------------------------------------------

    def _headers(self, server) -> dict[str, str]:
        headers = MCPServerRepository.headers_for(server)
        if self.secret_resolver is not None and hasattr(self.secret_resolver, "resolve_mapping"):
            headers = self.secret_resolver.resolve_mapping(headers)
        return headers

    # -- discovery ------------------------------------------------------------

    def local_tools(self, server) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "server_name": server.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
                "risk": tool.risk,
            }
            for tool in sandbox_tools_for(server.name)
        ]

    def _stdio_kwargs(self, server) -> dict[str, Any]:
        if not self.settings.toolloom_allow_stdio:
            raise MCPError(
                "stdio MCP servers are disabled; set TOOLLOOM_ALLOW_STDIO=1 to allow "
                "operator-configured local processes"
            )
        if not server.command:
            raise MCPError(f"stdio server {server.name!r} has no command configured")
        try:
            args = json.loads(server.args_json or "[]")
        except json.JSONDecodeError:
            args = []
        env = json.loads(server.env_json or "{}")
        if self.secret_resolver is not None:
            env = self.secret_resolver.resolve_mapping(env)
        return {"command": server.command, "args": args, "cwd": server.cwd, "env": env}

    async def discover_tools(self, server) -> list[dict[str, Any]]:
        transport = getattr(server, "transport", "http")
        if transport == "local":
            return self.local_tools(server)
        if transport in {"http", "sse", "stdio"}:
            if transport in {"http", "sse"} and not server.url:
                raise MCPError(f"server {server.name!r} has no url")
            extra = self._stdio_kwargs(server) if transport == "stdio" else {}
            tools = await mcp_list_tools(
                url=server.url,
                headers=self._headers(server),
                timeout_ms=server.timeout_ms,
                verify_ssl=server.verify_ssl,
                transport=transport,
                **extra,
            )
            return [
                {
                    "name": tool.name,
                    "server_name": server.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
                for tool in tools
            ]
        raise MCPError(f"unsupported transport {transport!r}")

    # -- dispatch -------------------------------------------------------------

    async def call_tool(
        self,
        server,
        tool_name: str,
        arguments: dict[str, Any],
        ctx,
    ) -> ToolCallOutcome:
        transport = getattr(server, "transport", "http")
        if transport == "local":
            if server.name == "sandbox" and tool_name == "python":
                from backend.services.sandbox.python_tool import execute
                return await execute(arguments, ctx, self.settings)
            # Local handlers may do blocking I/O (e.g. http_fetch); keep the loop free.
            text = await asyncio.to_thread(
                sandbox_dispatch, server.name, tool_name, arguments, ctx
            )
            return ToolCallOutcome(text=text, is_error=False)
        if transport in {"http", "sse", "stdio"}:
            if transport in {"http", "sse"} and not server.url:
                raise MCPError(f"server {server.name!r} has no url")
            extra = self._stdio_kwargs(server) if transport == "stdio" else {}
            return await mcp_call_tool(
                url=server.url,
                headers=self._headers(server),
                timeout_ms=server.timeout_ms,
                verify_ssl=server.verify_ssl,
                transport=transport,
                tool_name=tool_name,
                arguments=arguments,
                **extra,
            )
        raise MCPError(f"unsupported transport {transport!r}")

    async def test_connection(self, server) -> dict[str, Any]:
        tools = await self.discover_tools(server)
        return {"ok": True, "tool_count": len(tools)}
