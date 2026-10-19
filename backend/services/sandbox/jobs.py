"""Linux identity for guarded child processes that outlive a backend crash.

A normal cancellation kills the process group. An abrupt backend death does not,
so the child can keep writing after the run was recorded as interrupted. Each
spawned process is recorded with its start time, which is what lets a restart
distinguish the original process from a reused PID before terminating it.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

_lock = threading.Lock()
_dir: Path | None = None
_active: dict[str, dict] = {}


def _proc_start_time(pid: int) -> str | None:
    try:
        data = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    end = data.rfind(")")
    if end < 0:
        return None
    fields = data[end + 2 :].split()
    # starttime is field 22 overall, index 19 after the "(comm)" field.
    return fields[19] if len(fields) > 19 else None


def configure(path: Path | str) -> int:
    """Set the job directory and terminate processes left by a dead backend."""
    global _dir
    if sys.platform != "linux":
        return 0
    _dir = Path(path)
    _dir.mkdir(parents=True, exist_ok=True)
    return reconcile()


def register(pid: int, argv: list[str], cwd: str) -> str | None:
    if _dir is None or sys.platform != "linux":
        return None
    record = {
        "pid": pid,
        "pgid": pid,
        "start_time": _proc_start_time(pid),
        "argv": [str(item)[:200] for item in argv[:20]],
        "cwd": str(cwd),
        "created": time.time(),
    }
    path = _dir / f"{pid}-{os.getpid()}.json"
    try:
        with _lock:
            path.write_text(json.dumps(record), encoding="utf-8")
            _active[str(path)] = record
    except OSError:
        return None
    return str(path)


def unregister(token: str | None) -> None:
    if not token:
        return
    with _lock:
        _active.pop(token, None)
        try:
            Path(token).unlink(missing_ok=True)
        except OSError:
            pass


def reconcile() -> int:
    """Terminate recorded jobs whose identity still matches, then clear the records.

    A missing start time means the platform cannot verify identity, so the process
    is not killed: a reused PID is a worse outcome than a surviving orphan.
    """
    if _dir is None:
        return 0
    killed = 0
    for path in list(_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            pid = int(record["pid"])
            recorded = record.get("start_time")
        except (OSError, ValueError, KeyError, TypeError):
            path.unlink(missing_ok=True)
            continue
        current = _proc_start_time(pid)
        if recorded is None or current is None or current != str(recorded):
            path.unlink(missing_ok=True)
            continue
        try:
            os.killpg(int(record.get("pgid") or pid), signal.SIGKILL)
            killed += 1
        except OSError:
            pass
        path.unlink(missing_ok=True)
    return killed
