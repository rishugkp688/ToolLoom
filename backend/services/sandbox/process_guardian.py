"""Linux subreaper owns descendants even when they create new sessions.

Watches its backend parent so backend SIGKILL also triggers cleanup. This owns
ordinary tool processes; it does not isolate hostile code with our OS privileges.
"""

from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def _identity(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(fields[1]), fields[19]
    except (OSError, ValueError, IndexError):
        return None


def _descendants(pid):
    owner = _identity(pid)
    if owner is None:
        return []
    children = set()
    try:
        for task in Path(f"/proc/{pid}/task").iterdir():
            try:
                children.update(int(item) for item in (task / "children").read_text().split())
            except OSError:
                continue
    except OSError:
        return []
    owned = []
    for child_pid in children:
        identity = _identity(child_pid)
        if identity is not None and identity[0] == pid and _identity(pid) == owner:
            owned.extend(_descendants(child_pid))
            owned.append((child_pid, identity[1]))
    return owned


def _kill_owned():
    for pid, start_time in _descendants(os.getpid()):
        descriptor = None
        try:
            descriptor = os.pidfd_open(pid)
            identity = _identity(pid)
            if identity is not None and identity[1] == start_time:
                signal.pidfd_send_signal(descriptor, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except (AttributeError, OSError):
            identity = _identity(pid)
            if identity is not None and identity[1] == start_time:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        finally:
            if descriptor is not None:
                os.close(descriptor)


def main() -> int:
    parent_pid = int(sys.argv[1])
    arguments = sys.argv[2:]
    status_fd = None
    if arguments[:1] == ["--status-fd"]:
        status_fd = int(arguments[1])
        arguments = arguments[2:]
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "cannot establish process ownership")
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    # Popen closes the status descriptor in user code; only this guardian writes it.
    child = subprocess.Popen(arguments, stdin=subprocess.DEVNULL)
    shutdown_deadline = None
    code = None
    while True:
        if os.getppid() != parent_pid:
            stopping = True
        if stopping:
            shutdown_deadline = shutdown_deadline or time.monotonic() + 2
            _kill_owned()
        if code is None:
            code = child.poll()
            if code is not None and status_fd is not None:
                try:
                    payload = {
                        "exit_code": code,
                        "lingering_children": bool(_descendants(os.getpid())),
                    }
                    os.write(status_fd, json.dumps(payload).encode() + b"\n")
                except OSError:
                    stopping = True
                finally:
                    os.close(status_fd)
                    status_fd = None
        if code is not None:
            try:
                while os.waitpid(-1, os.WNOHANG)[0]:
                    pass
            except ChildProcessError:
                return code if code >= 0 else 128 - code
        if shutdown_deadline and time.monotonic() >= shutdown_deadline:
            _kill_owned()
            return 128 + signal.SIGTERM
        time.sleep(0.02)


if __name__ == "__main__":
    sys.exit(main())
