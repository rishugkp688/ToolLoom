"""Capabilities: the installed tools Tool Foundry produced.

Deliberately small. Listing, reading the source for review, and removing. There
is no second enable or approval concept here: those stay on the existing tool
endpoints (``PATCH /mcp/servers/{id}/tools/{name}``) so a generated tool behaves
exactly like every other tool in the registry.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from backend.services.tool_foundry.registry_adapter import GENERATED_SERVER
from backend.services.tool_foundry.store import find_installed, read_package_files, remove_installed

router = APIRouter(prefix="/capabilities", tags=["capabilities"])


def _registry(request: Request):
    return request.app.state.tool_registry


@router.get("")
def list_capabilities(request: Request) -> dict:
    registry = _registry(request)
    server = registry.get_server_by_name(GENERATED_SERVER)
    return {
        "server_id": server.id if server else None,
        "server_enabled": bool(server.enabled) if server else False,
        "foundry_enabled": request.app.state.settings.toolloom_allow_foundry,
        "capabilities": registry.generated_capabilities(),
    }


@router.get("/{name}/source")
def read_source(name: str, request: Request) -> dict:
    package = find_installed(request.app.state.settings, name)
    if package is None:
        raise HTTPException(status_code=404, detail="capability not found")
    return {
        "name": package.name,
        "version": package.version,
        "sha256": package.sha256,
        "integrity_ok": package.integrity_ok,
        "tests_passed": package.tests_passed,
        "source_conversation": package.source_conversation,
        "files": read_package_files(package),
    }


@router.delete("/{name}", status_code=204)
def remove_capability(name: str, request: Request) -> None:
    settings = request.app.state.settings
    if find_installed(settings, name) is None:
        raise HTTPException(status_code=404, detail="capability not found")
    remove_installed(settings, name)
    # Drop the tool rows so the catalogue stops advertising it immediately.
    _registry(request).sync_generated()
