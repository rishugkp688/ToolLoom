"""Bridge between installed capabilities and the tool registry.

Installed tools are registered under one server named ``generated``, using the
same enable flag, risk level and approval gate as every other tool. There is no
separate authorization concept for generated capabilities, and no per-tool MCP
server: one transport, one catalogue, one executor.
"""

from __future__ import annotations

from typing import Any

from backend.services.mcp_client import ToolCallOutcome
from backend.services.tool_foundry.runner import run_installed
from backend.services.tool_foundry.store import find_installed, list_installed

GENERATED_SERVER = "generated"
# Generated tools execute code the operator reviewed, so they never start low.
GENERATED_RISK = "high"


def discover(settings) -> list[dict[str, Any]]:
    """Tool catalogue for the ``generated`` server, read from installed packages."""
    return [
        {
            "name": package.name,
            "server_name": GENERATED_SERVER,
            "description": package.manifest.description,
            "input_schema": package.manifest.input_schema,
            "risk": GENERATED_RISK,
            # Approval is bound to this digest, so replacing the package revokes it.
            "digest": package.digest,
            # Generated capabilities run arbitrary code, so they stay unclassified
            # until an operator says otherwise: sequential, and never blindly retried.
            "effect": "unknown",
        }
        for package in list_installed(settings)
    ]


def describe(settings) -> list[dict[str, Any]]:
    """Everything the Capabilities view needs, including review evidence."""
    described: list[dict[str, Any]] = []
    for package in list_installed(settings):
        described.append(
            {
                "name": package.name,
                "tool": package.tool_name,
                "version": package.version,
                "description": package.manifest.description,
                "entrypoint": package.manifest.entrypoint,
                "input_schema": package.manifest.input_schema,
                "permissions": package.manifest.permissions.model_dump(),
                "sha256": package.sha256,
                "digest": package.digest,
                "integrity_ok": package.integrity_ok,
                "installed_at": package.installed_at,
                "source_conversation": package.source_conversation,
                "tests_passed": package.tests_passed,
            }
        )
    return described


async def call(settings, tool_name: str, arguments: dict[str, Any], ctx) -> ToolCallOutcome:
    package = find_installed(settings, tool_name)
    if package is None:
        return ToolCallOutcome(
            text=f"unknown generated tool {tool_name!r}; it may have been removed",
            is_error=True,
        )
    return await run_installed(package, arguments, ctx, settings)
