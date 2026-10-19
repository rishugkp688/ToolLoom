"""Async sandbox handlers for Tool Foundry.

These spawn subprocesses, so they run on the event loop through the same
dispatcher branch the Python tool uses, rather than in the worker thread the
synchronous sandbox handlers occupy.

The gate is ``TOOLLOOM_ALLOW_FOUNDRY``: validation executes generated code, so
the feature is off until the operator turns it on. A fresh install lands the tool
disabled and unapproved; an identical reinstall preserves the operator's existing
enable/approval state, and the install report states the state that actually
resulted rather than an install-time default.
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


def _activation_message(tool: str, state: dict) -> str:
    """Describe the state the catalogue is actually in, not an install default."""
    enabled = bool(state.get("enabled"))
    approved = bool(state.get("approved"))
    high_risk = state.get("risk") == "high"
    if enabled and (not high_risk or approved):
        return f"{tool} is installed, enabled and approved; the model can call it now."
    if enabled:
        return (
            f"{tool} is installed and enabled but NOT APPROVED; a high-risk call is "
            "refused until the operator approves it."
        )
    if approved:
        return (
            f"{tool} is installed and approved but DISABLED; enable it before the "
            "model can call it."
        )
    return (
        f"{tool} is installed but DISABLED and NOT APPROVED. The operator must "
        "enable and approve it in Settings -> Capabilities before any model can call it."
    )


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
    registry = getattr(ctx, "registry", None)
    if report.get("installed") and registry is not None:
        # Make the new tool visible immediately, then report the state that actually
        # resulted. A fresh install is disabled/unapproved, but an identical
        # reinstall preserves the operator's existing flags — the old static
        # "DISABLED and NOT APPROVED" text was wrong in that case.
        registry.sync_generated()
        state = registry.tool_state(report["tool"])
        if state is not None:
            report.update(state)
            report["activation"] = _activation_message(report["tool"], state)
    return ToolCallOutcome(text=_report(report, ok=report["ok"]), is_error=not report["ok"])


ASYNC_HANDLERS = {
    ("sandbox", "foundry_validate"): foundry_validate,
    ("sandbox", "foundry_install"): foundry_install,
}
