"""Async sandbox handlers for Tool Foundry.

These spawn subprocesses, so they run on the event loop through the same
dispatcher branch the Python tool uses, rather than in the worker thread the
synchronous sandbox handlers occupy.

The gate is ``TOOLLOOM_ALLOW_FOUNDRY``: validation executes generated code, so
the feature is off until the operator turns it on. Installation additionally
lands the tool disabled and unapproved.
"""

from __future__ import annotations

import json

from pydantic import ValidationError

from backend.services.mcp_client import ToolCallOutcome
from backend.services.tool_foundry import authoring
from backend.services.tool_foundry.manifest import FoundryError

DISABLED_MESSAGE = (
    "Tool Foundry is disabled. Set TOOLLOOM_ALLOW_FOUNDRY=1 in .env and restart "
    "the backend, then enable and approve the foundry tools under Settings -> MCP."
)


def _guard(settings) -> str | None:
    if not settings.toolloom_allow_foundry:
        return DISABLED_MESSAGE
    return None


def _report(payload: dict, *, ok: bool) -> str:
    return json.dumps(payload, indent=2, sort_keys=True)


async def foundry_validate(arguments, ctx, settings) -> ToolCallOutcome:
    blocked = _guard(settings)
    if blocked:
        return ToolCallOutcome(text=blocked, is_error=True)
    name = str(arguments.get("name") or "").strip()
    try:
        report = await authoring.validate_package_async(ctx.workspace(), name, settings)
    except (FoundryError, ValidationError) as exc:
        return ToolCallOutcome(text=f"cannot validate {name!r}: {exc}", is_error=True)
    return ToolCallOutcome(text=_report(report, ok=report["ok"]), is_error=not report["ok"])


async def foundry_install(arguments, ctx, settings) -> ToolCallOutcome:
    blocked = _guard(settings)
    if blocked:
        return ToolCallOutcome(text=blocked, is_error=True)
    name = str(arguments.get("name") or "").strip()
    try:
        report = await authoring.install_from_workspace(
            ctx.workspace(), name, settings, ctx.conversation_id
        )
    except (FoundryError, ValidationError) as exc:
        return ToolCallOutcome(text=f"cannot install {name!r}: {exc}", is_error=True)
    if report.get("installed") and getattr(ctx, "registry", None) is not None:
        # Make the new tool visible immediately; it still arrives disabled.
        ctx.registry.sync_generated()
    return ToolCallOutcome(text=_report(report, ok=report["ok"]), is_error=not report["ok"])


ASYNC_HANDLERS = {
    ("sandbox", "foundry_validate"): foundry_validate,
    ("sandbox", "foundry_install"): foundry_install,
}
