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
import shutil
import signal
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

# Kept deliberately small. HOME is excluded so a tool cannot quietly write into
# the operator's home directory by expanding a path. Callers that genuinely need
# a home directory (the shell tool, for toolchain caches) add it explicitly.
_ENV_ALLOWLIST = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")


def find_posix_shell() -> list[str] | None:
    """The interpreter flags for a POSIX shell, or None when there is no shell.

    bash is preferred over sh for its predictable builtins. Windows without Git
    Bash or WSL has neither, and callers must say so rather than falling back to
    cmd, whose semantics differ enough to make the same command mean something
    else.

    ``pipefail`` is set deliberately. A pipeline reports the status of its *last*
    command, so the very common ``pytest | tail -20`` would report success while
    the tests were failing — the tool would hand the model an ``exit_code`` of 0
    next to a screen of failures, and a weak model reads the exit code. With
    pipefail the real failure surfaces, so ``is_error`` is trustworthy for piped
    commands. ``sh`` has no pipefail, so that guarantee is bash-only.
    """
    bash = shutil.which("bash")
    if bash:
        return [bash, "-o", "pipefail", "-c"]
    sh = shutil.which("sh")
    if sh:
        return [sh, "-c"]
    return None


@dataclass
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int | None
    # "exited" | "timeout"
    stop_reason: str
    # True when the retained output hit the cap. The command still ran to
    # completion: killing a build because its log is noisy is a worse outcome than
    # keeping a bounded view of it.
    truncated: bool = False

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


class _Retained:
    """A bounded view of one stream: its head and its tail, never only its head.

    A long stdout preamble used to consume the whole allowance, so the final error
    on stderr was discarded. Each stream now has its own budget and keeps both ends.
    """

    def __init__(self, cap: int) -> None:
        self.cap = max(1, cap)
        self.head_cap = self.cap // 2
        self.tail_cap = self.cap - self.head_cap
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def feed(self, data: bytes) -> None:
        self.total += len(data)
        if len(self.head) < self.head_cap:
            take = min(len(data), self.head_cap - len(self.head))
            self.head.extend(data[:take])
            data = data[take:]
        if data and self.tail_cap:
            self.tail.extend(data)
            if len(self.tail) > self.tail_cap:
                del self.tail[: len(self.tail) - self.tail_cap]

    @property
    def truncated(self) -> bool:
        return self.total > self.cap

    def text(self) -> str:
        return bytes(self.head + self.tail).decode("utf-8", "replace")


async def run_argv(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout: float,
    max_bytes: int,
) -> ExecResult:
    """Run a command, bounded by wall clock and combined output size."""
    owned_argv = argv
    if sys.platform == "linux":
        owned_argv = [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("process_guardian.py")),
            *argv,
        ]
    process = await asyncio.create_subprocess_exec(
        *owned_argv,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=os.name == "posix",
    )
    stdout_cap = max(1, int(max_bytes * 0.75))
    stderr_cap = max(1, max_bytes - stdout_cap)
    stdout, stderr = _Retained(stdout_cap), _Retained(stderr_cap)
    reason: str | None = None

    job_registry = None
    job = None
    try:
        from backend.services.sandbox import jobs as job_registry

        job = job_registry.register(process.pid, argv, cwd)
    except Exception:
        job_registry = None
        job = None

    async def read(stream, sink: _Retained) -> None:
        while chunk := await stream.read(4096):
            sink.feed(chunk)

    tasks = [
        asyncio.create_task(read(process.stdout, stdout)),
        asyncio.create_task(read(process.stderr, stderr)),
        asyncio.create_task(process.wait()),
    ]
    try:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=timeout)
    except TimeoutError:
        reason = "timeout"
    finally:
        # Cancellation must terminate execution too, not leave a background writer.
        import anyio

        with anyio.CancelScope(shield=True):
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await terminate(process)
            if job_registry is not None:
                job_registry.unregister(job)
    return ExecResult(
        stdout=stdout.text(),
        stderr=stderr.text(),
        exit_code=process.returncode,
        stop_reason=reason or "exited",
        truncated=stdout.truncated or stderr.truncated,
    )
