from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, HTTPException, Request

from backend.schemas.requests import (
    CreateMCPServerRequest,
    UpdateMCPServerRequest,
    UpdateToolRequest,
)
from backend.schemas.responses import DiscoveryResponse, MCPServerOut, ToolOut
from backend.services.mcp_connection_manager import MCPError
from backend.services.secrets import SecretsError
from backend.services.tool_registry import RegistryError
from backend.utils.errors import flatten_error

router = APIRouter(prefix="/mcp", tags=["mcp"])


def _registry(request: Request):
    return request.app.state.tool_registry


def _connection_error(exc: Exception, action: str) -> str:
    kind = type(flatten_error(exc)).__name__
    return f"MCP {action} failed ({kind}); check the connection, credentials, and timeout."


def _server_out(server) -> MCPServerOut:
    try:
        header_names = sorted(json.loads(server.headers_json or "{}").keys())
    except (json.JSONDecodeError, AttributeError):
        header_names = []
    return MCPServerOut(
        id=server.id,
        name=server.name,
        transport=server.transport,
        url=(
            urlunsplit(urlsplit(server.url)._replace(query="[redacted]"))
            if server.url and urlsplit(server.url).query
            else server.url
        ),
        header_names=header_names,
        enabled=server.enabled,
        trusted=server.trusted,
        verify_ssl=server.verify_ssl,
        timeout_ms=server.timeout_ms,
    )


def _tool_out(tool) -> ToolOut:
    try:
        schema = json.loads(tool.input_schema_json or "{}")
    except json.JSONDecodeError:
        schema = {}
    return ToolOut(
        id=tool.id,
        server_id=tool.server_id,
        name=tool.name,
        namespaced_name=tool.namespaced_name,
        description=tool.description,
        input_schema=schema,
        enabled=tool.enabled,
        risk=getattr(tool, "risk", "low"),
        approved=getattr(tool, "approved", False),
        effect=getattr(tool, "effect", "unknown") or "unknown",
    )


@router.get("/servers", response_model=list[MCPServerOut])
def list_servers(request: Request) -> list[MCPServerOut]:
    return [_server_out(server) for server in _registry(request).list_servers()]


@router.post("/servers", response_model=MCPServerOut, status_code=201)
def create_server(body: CreateMCPServerRequest, request: Request) -> MCPServerOut:
    try:
        server = _registry(request).create_server(
            name=body.name,
            transport=body.transport,
            url=body.url,
            headers=body.headers,
            enabled=body.enabled,
            trusted=body.trusted,
            verify_ssl=body.verify_ssl,
            timeout_ms=body.timeout_ms,
            command=body.command,
            args=body.args,
            cwd=body.cwd,
            env=body.env,
        )
    except RegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _server_out(server)


@router.patch("/servers/{server_id}", response_model=MCPServerOut)
def update_server(server_id: str, body: UpdateMCPServerRequest, request: Request) -> MCPServerOut:
    fields: dict[str, Any] = {}
    if body.enabled is not None:
        fields["enabled"] = body.enabled
    if body.url is not None:
        fields["url"] = body.url
    if body.headers is not None:
        fields["headers_json"] = body.headers
    if body.verify_ssl is not None:
        fields["verify_ssl"] = body.verify_ssl
    if body.timeout_ms is not None:
        fields["timeout_ms"] = body.timeout_ms
    try:
        server = _registry(request).update_server(server_id, **fields)
    except RegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if server is None:
        raise HTTPException(status_code=404, detail="server not found")
    return _server_out(server)


@router.delete("/servers/{server_id}", status_code=204)
def delete_server(server_id: str, request: Request) -> None:
    try:
        deleted = _registry(request).delete_server(server_id)
    except RegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="server not found")


@router.post("/servers/import")
async def import_servers(request: Request, sync: bool = False) -> dict:
    chunks = bytearray()
    async for chunk in request.stream():
        chunks.extend(chunk)
        if len(chunks) > 512 * 1024:
            raise HTTPException(status_code=413, detail="config exceeds 512 KB")
    raw = chunks.decode("utf-8-sig", "replace")
    if not raw.strip():
        raise HTTPException(status_code=400, detail="empty import body")
    try:
        result = _registry(request).import_config(raw, sync=sync)
        if not result.get("applied"):
            from fastapi.responses import JSONResponse

            errors = [
                f"{row['name']}: {row.get('error', 'invalid configuration')}"
                for row in result.get("results", []) if not row.get("ok")
            ]
            return JSONResponse(status_code=400, content={
                **result, "detail": "Nothing changed. " + "; ".join(errors),
                "category": "mcp_config_validation",
            })
        return result
    except RegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:  # JSONDecodeError is a ValueError subclass
        raise HTTPException(status_code=400, detail=f"invalid config: {exc}") from exc


@router.get("/config")
def current_config(request: Request) -> dict:
    return _registry(request).export_config()


@router.post("/servers/{server_id}/discover", response_model=DiscoveryResponse)
async def discover(server_id: str, request: Request) -> DiscoveryResponse:
    try:
        result = await _registry(request).discover(server_id)
    except RegistryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (MCPError, SecretsError) as exc:
        raise HTTPException(
            status_code=502,
            detail=_connection_error(exc, "discovery"),
        ) from exc
    return DiscoveryResponse(
        server_id=result.server_id,
        added=result.added,
        updated=result.updated,
        removed=result.removed,
        tools=[_tool_out(tool) for tool in result.tools],
    )


@router.post("/servers/{server_id}/test")
async def test_connection(server_id: str, request: Request) -> dict:
    try:
        return await _registry(request).test_connection(server_id)
    except RegistryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (MCPError, SecretsError) as exc:
        raise HTTPException(
            status_code=502,
            detail=_connection_error(exc, "connection"),
        ) from exc


@router.get("/servers/{server_id}/tools", response_model=list[ToolOut])
def list_tools(server_id: str, request: Request) -> list[ToolOut]:
    if _registry(request).get_server(server_id) is None:
        raise HTTPException(status_code=404, detail="server not found")
    return [_tool_out(tool) for tool in _registry(request).list_tools(server_id)]


@router.patch("/servers/{server_id}/tools/{tool_name}", response_model=ToolOut)
def update_tool(
    server_id: str, tool_name: str, body: UpdateToolRequest, request: Request
) -> ToolOut:
    fields: dict[str, Any] = {}
    if body.enabled is not None:
        fields["enabled"] = body.enabled
    if body.approved is not None:
        fields["approved"] = body.approved
    if body.risk is not None:
        fields["risk"] = body.risk
    if body.effect is not None:
        fields["effect"] = body.effect
    try:
        tool = _registry(request).update_tool(server_id, tool_name, **fields)
    except RegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if tool is None:
        raise HTTPException(status_code=404, detail="tool not found")
    return _tool_out(tool)
