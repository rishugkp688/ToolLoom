"""Explicitly opted-in Python execution. This is NOT an OS security sandbox.

The process runs as the operator with a workspace cwd and a minimal environment.
It can still access host files/network. Time/output bounds protect normal runs;
malicious code requires actual OS/container isolation, outside this feature.

The subprocess policy itself lives in ``_exec`` and is shared with Tool Foundry.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from backend.services.mcp_client import ToolCallOutcome
from backend.services.sandbox._exec import minimal_env, run_argv
from backend.services.tool_errors import ToolPreflightError

MAX_CODE_CHARS = 32_000


async def execute(arguments, context, settings) -> ToolCallOutcome:
    if not settings.toolloom_allow_python:
        raise ToolPreflightError("Python is disabled; set TOOLLOOM_ALLOW_PYTHON=1 and restart")
    code = arguments.get("code", "")
    if not isinstance(code, str) or not code.strip() or len(code) > MAX_CODE_CHARS:
        raise ToolPreflightError(f"code must contain 1–{MAX_CODE_CHARS} characters")
    from backend.services.sandbox.shell_tool import _timeout

    timeout = _timeout(arguments, settings.toolloom_python_timeout_seconds)
    result = await run_argv(
        [sys.executable, "-I", "-u", "-X", "utf8", "-c", code],
        cwd=str(context.workspace().root),
        env=minimal_env(),
        timeout=timeout,
        max_bytes=settings.toolloom_python_output_bytes,
        capture_dir=Path(context.workspace().root),
        secrets=getattr(context, "output_secrets", ()),
    )
    payload = {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code,
        "stop_reason": result.stop_reason,
        "truncated": result.truncated,
        "requested_timeout_seconds": arguments.get("timeout_seconds"),
        "effective_timeout_seconds": timeout,
        "lingering_children": result.lingering_children,
        "cleanup_error": result.cleanup_error,
        "output_captures": result.captures,
    }
    timed_out = result.stop_reason == "timeout"
    if timed_out:
        payload["note"] = f"stopped after {timeout}s; output above is partial"
    # A non-zero exit is a result, not a broken call: `sys.exit(7)` and a failing
    # assertion both mean the code ran. Only a timeout is a failure to execute.
    return ToolCallOutcome(
        text=json.dumps(payload),
        is_error=timed_out,
        outcome_uncertain=timed_out or result.cleanup_error is not None,
        exit_code=result.exit_code,
        verification_failed=(not timed_out and result.exit_code not in (None, 0)),
    )
