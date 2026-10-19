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
import json
import os
import shutil
import signal
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import anyio

from backend.services.sandbox.capture import Capture, Redactor
from backend.utils.async_work import run_blocking

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
    lingering_children: bool = False
    cleanup_error: str | None = None
    captures: dict | None = None

    @property
    def ok(self) -> bool:
        return self.stop_reason == "exited" and self.exit_code == 0 and self.cleanup_error is None


def minimal_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {name: os.environ[name] for name in _ENV_ALLOWLIST if name in os.environ}
    env.update(
        PYTHONIOENCODING="utf-8",
        PYTHONUNBUFFERED="1",
        NO_COLOR="1",
        TERM="dumb",
        PAGER="cat",
        GIT_PAGER="cat",
        GIT_TERMINAL_PROMPT="0",
    )
    if extra:
        env.update(extra)
    return env


async def terminate(process, *, drain: bool = True) -> None:
    """Kill a child and any descendants, then drain its pipes."""
    if os.name == "posix":
        if sys.platform == "linux" and process.returncode is None:
            # The subreaper must clean up children in other sessions before its
            # group is killed, otherwise detached daemons lose their owner.
            with suppress(ProcessLookupError):
                process.send_signal(signal.SIGTERM)
            with suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(process.wait()), 3)
        # The child is its own session leader, so the pgid takes descendants too.
        if sys.platform != "linux" or process.returncode is None:
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
    if drain:
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
        if self.truncated:
            # Each end can fall inside a UTF-8 character; omit only those fragments.
            return self.head.decode("utf-8", "ignore") + self.tail.decode("utf-8", "ignore")
        return bytes(self.head + self.tail).decode("utf-8", "replace")


async def _finish_owned(task):
    """Finish cleanup even through repeated raw asyncio cancellation."""
    interrupted = False
    with anyio.CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                interrupted = True
            except Exception:
                break
    if interrupted:
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError
    return task.result()


async def _spawn_owned(argv, **kwargs):
    # Cancellation may arrive after the OS spawn but before asyncio delivers the
    # Process. Retain that delivery and reap it before releasing ownership.
    launch = asyncio.create_task(asyncio.create_subprocess_exec(*argv, **kwargs))
    try:
        return await asyncio.shield(launch)
    except asyncio.CancelledError:
        with anyio.CancelScope(shield=True):
            while not launch.done():
                try:
                    await asyncio.shield(launch)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not launch.cancelled() and launch.exception() is None:
                process = launch.result()

                async def reap():
                    try:
                        await terminate(process)
                    except Exception:
                        from backend.services.sandbox import jobs

                        jobs.register(
                            process.pid, argv, kwargs["cwd"], guarded=sys.platform == "linux"
                        )

                await _finish_owned(asyncio.create_task(reap()))
        raise


async def run_argv(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout: float,
    max_bytes: int,
    capture_dir: Path | None = None,
    secrets: tuple[str, ...] = (),
) -> ExecResult:
    """Run a command, bounded by wall clock and combined output size."""
    owned_argv = argv
    status_read = status_write = None
    extra = {}
    if sys.platform == "linux":
        status_read, status_write = os.pipe()
        os.set_blocking(status_read, False)
        extra["pass_fds"] = (status_write,)
        owned_argv = [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("process_guardian.py")),
            str(os.getpid()),
            "--status-fd",
            str(status_write),
            *argv,
        ]
    try:
        process = await _spawn_owned(
            owned_argv,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name == "posix",
            **extra,
        )
    except BaseException:
        if status_read is not None:
            os.close(status_read)
        raise
    finally:
        if status_write is not None:
            os.close(status_write)
    stdout_cap = max(1, int(max_bytes * 0.75))
    stderr_cap = max(1, max_bytes - stdout_cap)
    stdout, stderr = _Retained(stdout_cap), _Retained(stderr_cap)
    reason: str | None = None
    root = {}
    cleanup_error = None
    captures = (
        {name: Capture(Path(capture_dir), name) for name in ("stdout", "stderr")}
        if capture_dir is not None
        else {}
    )
    eof = {"stdout": False, "stderr": False}

    job_registry = None
    job = None
    try:
        from backend.services.sandbox import jobs as job_registry

        job = job_registry.register(process.pid, argv, cwd, guarded=sys.platform == "linux")
    except Exception:
        job_registry = None
        job = None

    async def read(stream, sink: _Retained, name: str) -> None:
        redactor = Redactor(secrets)
        try:
            while chunk := await stream.read(16_384):
                text = redactor.feed(chunk)
                sink.feed(text.encode("utf-8"))
                if name in captures:
                    await run_blocking(captures[name].feed, text)
            eof[name] = True
            if name in captures:
                captures[name].complete = True
        finally:
            text = redactor.feed(final=True)
            sink.feed(text.encode("utf-8"))
            if name in captures:
                await run_blocking(captures[name].feed, text)

    async def root_exited():
        data = b""
        while True:
            if status_read is not None:
                try:
                    part = os.read(status_read, 4096)
                except BlockingIOError:
                    part = None
                if part:
                    data += part
                    if b"\n" in data:
                        root.update(json.loads(data.split(b"\n", 1)[0]))
                        return
                elif part == b"":
                    # A guardian launch failure still has a real process result.
                    if process.returncode is not None:
                        return
            elif process.returncode is not None:
                root["exit_code"] = process.returncode
                return
            await asyncio.sleep(0.01)

    tasks = [
        asyncio.create_task(read(process.stdout, stdout, "stdout")),
        asyncio.create_task(read(process.stderr, stderr, "stderr")),
    ]
    root_task = asyncio.create_task(root_exited())
    try:
        await asyncio.wait_for(asyncio.shield(root_task), timeout=timeout)
        # Root completion is authoritative. EOF and the guardian can lag because
        # descendants inherited the streams, including descendants in new sessions.
        try:
            async with asyncio.timeout(0.25):
                await asyncio.gather(*(asyncio.shield(task) for task in tasks))
                await process.wait()
        except TimeoutError:
            # On Linux the guardian knows whether there were children. A slow
            # disk/reader can also delay EOF processing; do not invent a child.
            root.setdefault("lingering_children", sys.platform != "linux")
    except TimeoutError:
        reason = "timeout"
    finally:
        # Cancellation must terminate execution too, not leave a background writer.
        async def cleanup_owned():
            nonlocal cleanup_error
            try:
                await terminate(process, drain=False)
                async with asyncio.timeout(1):
                    await asyncio.gather(*(asyncio.shield(task) for task in tasks))
                    await process.wait()
            except (OSError, TimeoutError, RuntimeError) as exc:
                cleanup_error = type(exc).__name__
            finally:
                for task in [*tasks, root_task]:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, root_task, return_exceptions=True)
                for capture in captures.values():
                    capture.close()
                if status_read is not None:
                    os.close(status_read)
            if job_registry is not None and cleanup_error is None:
                job_registry.unregister(job)

        await _finish_owned(asyncio.create_task(cleanup_owned()))
    return ExecResult(
        stdout=stdout.text(),
        stderr=stderr.text(),
        exit_code=root.get("exit_code", process.returncode),
        stop_reason=reason or "exited",
        truncated=stdout.truncated or stderr.truncated or not all(eof.values()),
        lingering_children=bool(root.get("lingering_children")),
        cleanup_error=cleanup_error,
        captures={name: item.metadata() for name, item in captures.items()} or None,
    )
