"""Operator-opted-in shell execution. This is NOT an OS security sandbox.

This tool exists so the agent can verify its own work: run the test suite, the
build or the linter, read the failure, and fix it. Without something like it an
agent can write a project but never check that it works, which makes "done"
unfalsifiable.

The command runs through a real shell as the operator, in the conversation
workspace by default. It can read and write anything you can. ``cwd`` may also
point outside the workspace, deliberately: the command could ``cd`` there anyway,
so confining the argument would be a false guarantee rather than a real boundary.
Time and output bounds protect ordinary runs; they are not containment.

Like the Python tool, the child gets a minimal environment rather than the
operator's, so provider keys and vault material cannot leak into a command.
``HOME`` is the one addition: real toolchains (npm, pip, git) need it for caches
and config, and withholding it would break normal use without containing anything.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from backend.services.mcp_client import ToolCallOutcome
from backend.services.sandbox._exec import find_posix_shell, minimal_env, run_argv

MAX_COMMAND_CHARS = 8_000


def shell_available() -> bool:
    """Whether a POSIX shell exists, so the tool is not advertised if it cannot run."""
    return find_posix_shell() is not None


def _timeout(arguments: dict, ceiling: int) -> float:
    requested = arguments.get("timeout_seconds")
    if isinstance(requested, (int, float)) and not isinstance(requested, bool):
        return float(max(1, min(int(requested), ceiling)))
    return float(ceiling)


def _working_directory(arguments: dict, default: Path) -> Path:
    requested = arguments.get("cwd")
    if not requested:
        return default
    candidate = Path(str(requested)).expanduser()
    if not candidate.is_absolute():
        candidate = default / candidate
    if not candidate.is_dir():
        raise ValueError(f"working directory not found: {requested}")
    return candidate


async def execute(arguments, context, settings) -> ToolCallOutcome:
    if not settings.toolloom_allow_shell:
        raise RuntimeError("Shell execution is disabled; set TOOLLOOM_ALLOW_SHELL=1 and restart")

    command = arguments.get("command", "")
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command must be a non-empty string")
    if len(command) > MAX_COMMAND_CHARS:
        raise ValueError(f"command must be at most {MAX_COMMAND_CHARS} characters")

    shell = find_posix_shell()
    if shell is None:
        raise RuntimeError(
            "no POSIX shell found on PATH; shell execution needs bash or sh "
            "(on Windows, install Git Bash or use WSL)"
        )

    cwd = _working_directory(arguments, Path(context.workspace().root))
    env = minimal_env({"HOME": os.environ["HOME"]} if "HOME" in os.environ else None)
    result = await run_argv(
        [*shell, command],
        cwd=str(cwd),
        env=env,
        timeout=_timeout(arguments, settings.toolloom_shell_timeout_seconds),
        max_bytes=settings.toolloom_shell_output_bytes,
    )
    return ToolCallOutcome(
        text=json.dumps(
            {
                "command": command,
                "cwd": str(cwd),
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.exit_code,
                "stop_reason": result.stop_reason,
                "truncated": result.truncated,
            }
        ),
        is_error=not result.ok,
    )
