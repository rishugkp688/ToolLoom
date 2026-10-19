"""Running an installed capability.

Arguments arrive already validated: ``ToolExecutor`` checks them against the
stored JSON schema, which was populated from the manifest, before dispatch. So
this module only maps arguments onto CLI flags and runs the entrypoint.

Two decisions worth stating:

* **argv arrays, never a shell.** A flag value is passed as one argument, so a
  value containing spaces or metacharacters cannot become a second command.
* **cwd is the conversation workspace.** A tool takes workspace-relative paths
  such as ``--input security-events.jsonl``, which is what makes it usable from
  a conversation rather than only from its own directory.

Not isolation: the child runs as the operator.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from backend.services.mcp_client import ToolCallOutcome
from backend.services.sandbox._exec import minimal_env, run_argv
from backend.services.tool_foundry.store import InstalledPackage, verify_integrity

MAX_STDERR_CHARS = 2_000


def _scalar(value: Any) -> str:
    if isinstance(value, (str, int, float)):
        return str(value)
    return json.dumps(value, sort_keys=True)


def build_argv(package: InstalledPackage, arguments: dict[str, Any]) -> list[str]:
    """Map schema properties onto ``--flag value`` pairs, in a stable order."""
    properties = package.manifest.input_schema.get("properties") or {}
    argv = [sys.executable, "-s", "-u", "-X", "utf8", str(package.entrypoint)]
    if getattr(package.manifest, "argument_encoding", "flags") == "json":
        return [*argv, "--toolloom-json", json.dumps(arguments, allow_nan=False)]
    for key in sorted(properties):
        if key not in arguments:
            continue
        value = arguments[key]
        flag = "--" + key.replace("_", "-")
        if isinstance(value, bool):
            if value:
                argv.append(flag)
        elif value is None:
            continue
        elif isinstance(value, list):
            for item in value:
                argv.append(f"{flag}={_scalar(item)}")
        else:
            argv.append(f"{flag}={_scalar(value)}")
    return argv


def _failure(package: InstalledPackage, result) -> ToolCallOutcome:
    if result.stop_reason == "timeout":
        return ToolCallOutcome(
            text=f"{package.manifest.name} timed out and was terminated",
            is_error=True,
            outcome_uncertain=True,
        )
    if result.stop_reason == "output_limit":
        return ToolCallOutcome(
            text=f"{package.manifest.name} exceeded the output limit and was terminated",
            is_error=True,
            outcome_uncertain=True,
        )
    detail = result.stderr.strip() or result.stdout.strip() or "(no output)"
    return ToolCallOutcome(
        text=f"{package.manifest.name} exited with code {result.exit_code}\n\n"
        f"{detail[-MAX_STDERR_CHARS:]}",
        is_error=True,
        exit_code=result.exit_code,
        verification_failed=True,
    )


async def run_installed(
    package: InstalledPackage, arguments: dict[str, Any], ctx, settings
) -> ToolCallOutcome:
    if not verify_integrity(package):
        return ToolCallOutcome(
            text=(
                f"refusing to run {package.tool_name}: the installed package no longer matches "
                "the digest recorded at install. Reinstall it or remove it before trying again."
            ),
            is_error=True,
        )

    timeout = float(package.manifest.timeout_seconds or settings.toolloom_foundry_timeout_seconds)
    result = await run_argv(
        build_argv(package, arguments),
        cwd=str(ctx.workspace().root),
        env=minimal_env({"PYTHONPATH": str(package.entrypoint.parent)}),
        timeout=timeout,
        max_bytes=settings.toolloom_foundry_output_bytes,
    )
    if not result.ok:
        return _failure(package, result)
    return ToolCallOutcome(text=result.stdout.strip() or "(no output)", is_error=False)
