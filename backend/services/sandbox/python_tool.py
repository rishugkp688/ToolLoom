"""Explicitly opted-in Python execution. This is NOT an OS security sandbox.

The process runs as the operator with a workspace cwd and a minimal environment.
It can still access host files/network. Time/output bounds protect normal runs;
malicious code requires actual OS/container isolation, outside this feature.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from contextlib import suppress

import anyio

from backend.services.mcp_client import ToolCallOutcome


async def _terminate(process) -> None:
    if os.name == "posix":
        # Also clean ordinary descendants after the direct child exits.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    elif process.returncode is None:  # pragma: no cover - Windows
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), 5)
        finally:
            with suppress(ProcessLookupError):
                process.kill()
    # Drain buffered pipes after termination; wait() alone can deadlock when full.
    await asyncio.wait_for(process.communicate(), 5)


async def execute(arguments, context, settings) -> ToolCallOutcome:
    if not settings.toolloom_allow_python:
        raise RuntimeError("Python is disabled; set TOOLLOOM_ALLOW_PYTHON=1 and restart")
    code = arguments.get("code", "")
    if not isinstance(code, str) or not code.strip() or len(code) > 32_000:
        raise ValueError("code must contain 1–32000 characters")
    timeout = min(
        arguments.get("timeout_seconds", settings.toolloom_python_timeout_seconds),
        settings.toolloom_python_timeout_seconds,
    )
    env = {
        k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP") if k in os.environ
    }
    env.update(PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-u",
        "-X",
        "utf8",
        "-c",
        code,
        cwd=str(context.workspace().root),
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=os.name == "posix",
    )
    stdout, stderr = bytearray(), bytearray()
    total = 0
    max_bytes = settings.toolloom_python_output_bytes
    reason = None

    async def read(stream, output):
        nonlocal total
        while chunk := await stream.read(4096):
            remaining = max(0, max_bytes - total)
            output.extend(chunk[:remaining])
            total += len(chunk)
            if total > max_bytes:
                raise OverflowError("output limit exceeded")

    tasks = [
        asyncio.create_task(read(process.stdout, stdout)),
        asyncio.create_task(read(process.stderr, stderr)),
        asyncio.create_task(process.wait()),
    ]
    try:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=timeout)
    except TimeoutError:
        reason = "timeout"
    except OverflowError:
        reason = "output_limit"
    finally:
        # Cancellation must terminate execution too, not leave a background writer.
        with anyio.CancelScope(shield=True):
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await _terminate(process)
    return ToolCallOutcome(
        text=json.dumps(
            {
                "stdout": stdout.decode("utf-8", "replace"),
                "stderr": stderr.decode("utf-8", "replace"),
                "exit_code": process.returncode,
                "stop_reason": reason or "exited",
            }
        ),
        is_error=reason is not None or process.returncode != 0,
    )
