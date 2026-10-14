"""One subprocess policy for every in-process tool that spawns a child.

Shared by the Python tool and Tool Foundry, so both get the same rules:

* argv arrays only. Nothing is ever passed through a shell.
* a minimal environment built from scratch, never the operator's full env.
* a hard wall-clock timeout, with the whole process tree killed on expiry.
* a combined stdout/stderr byte cap enforced while reading, so a runaway
  writer cannot exhaust memory before the cap applies.

This is not OS isolation and does not claim to be. A child still runs as the
operator. These bounds protect normal runs, not adversarial ones.
"""

from __future__ import annotations

import asyncio
import os
import signal
from contextlib import suppress
from dataclasses import dataclass

# Kept deliberately small. HOME is excluded so a tool cannot quietly write into
# the operator's home directory by expanding a path.
_ENV_ALLOWLIST = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")


@dataclass
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int | None
    # "exited" | "timeout" | "output_limit"
    stop_reason: str

    @property
    def ok(self) -> bool:
        return self.stop_reason == "exited" and self.exit_code == 0


def minimal_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {name: os.environ[name] for name in _ENV_ALLOWLIST if name in os.environ}
    env.update(PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    if extra:
        env.update(extra)
    return env


async def terminate(process) -> None:
    """Kill a child and any descendants, then drain its pipes."""
    if os.name == "posix":
        # The child is its own session leader, so the pgid takes descendants too.
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


async def run_argv(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout: float,
    max_bytes: int,
) -> ExecResult:
    """Run a command, bounded by wall clock and combined output size."""
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=os.name == "posix",
    )
    stdout, stderr = bytearray(), bytearray()
    total = 0
    reason: str | None = None

    async def read(stream, sink: bytearray) -> None:
        nonlocal total
        while chunk := await stream.read(4096):
            remaining = max(0, max_bytes - total)
            sink.extend(chunk[:remaining])
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
        import anyio

        with anyio.CancelScope(shield=True):
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await terminate(process)
    return ExecResult(
        stdout=stdout.decode("utf-8", "replace"),
        stderr=stderr.decode("utf-8", "replace"),
        exit_code=process.returncode,
        stop_reason=reason or "exited",
    )
