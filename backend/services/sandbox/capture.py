"""Capped UTF-8 captures. Redaction happens before both memory and disk views.

These are ordinary runtime-owned files, not an OS isolation boundary. Workspace
quotas include completed captures; once full, stop saving and report dropped bytes.
"""

from __future__ import annotations

import codecs
import re
import threading
import uuid
from pathlib import Path

STREAM_CAP = 16 * 1024 * 1024
WORKSPACE_CAP = 128 * 1024 * 1024
MAX_CAPTURE_FILES = 2048
_LOCK = threading.Lock()


class Redactor:
    def __init__(self, secrets=()):
        values = sorted({value for value in secrets if value}, key=len, reverse=True)
        self.pattern = (
            re.compile("|".join(re.escape(value) for value in values)) if values else None
        )
        self.keep = max((len(value) for value in values), default=1) - 1
        self.pending = ""
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def feed(self, data: bytes = b"", *, final=False) -> str:
        self.pending += self.decoder.decode(data, final=final)
        end = len(self.pending) if final else max(0, len(self.pending) - self.keep)
        parts = []
        position = 0
        if self.pattern:
            for match in self.pattern.finditer(self.pending):
                if match.start() >= end:
                    break
                parts.extend((self.pending[position : match.start()], "[REDACTED]"))
                position = match.end()
        cut = max(position, end)
        parts.append(self.pending[position:cut])
        self.pending = self.pending[cut:]
        return "".join(parts)


class Capture:
    def __init__(self, workspace: Path, stream: str):
        self.path = None
        self.handle = None
        self.saved = 0
        self.total = 0
        self.complete = False
        self.error = None
        self.limit = 0
        self.workspace = workspace
        try:
            directory = workspace / ".toolloom" / "captures"
            directory.mkdir(parents=True, exist_ok=True)
            with _LOCK:
                files = list(directory.iterdir())
                if len(files) >= MAX_CAPTURE_FILES:
                    self.error = "capture_file_quota_reached"
                    return
                available = WORKSPACE_CAP - sum(p.stat().st_size for p in files if p.is_file())
                self.limit = min(STREAM_CAP, max(0, available))
                if not self.limit:
                    self.error = "workspace_capture_quota_reached"
                    return
                self.path = directory / f"{uuid.uuid4().hex}-{stream}.txt"
                self.handle = self.path.open("xb")
        except OSError as exc:
            self.error = type(exc).__name__ + ": capture unavailable"

    def feed(self, text: str):
        data = text.encode("utf-8")
        self.total += len(data)
        if self.handle is None:
            return
        try:
            with _LOCK:
                # Recheck the shared workspace allowance as other streams grow.
                used = sum(p.stat().st_size for p in self.path.parent.iterdir() if p.is_file())
                room = min(self.limit - self.saved, max(0, WORKSPACE_CAP - used))
                piece = data[:room].decode("utf-8", "ignore").encode("utf-8")
                self.handle.write(piece)
                self.handle.flush()
                self.saved += len(piece)
                if len(piece) < len(data):
                    self.handle.close()
                    self.handle = None
        except OSError:
            self.error = "OSError: capture write failed"
            self.close()

    def close(self):
        if self.handle is not None:
            try:
                self.handle.close()
            except OSError:
                self.error = "OSError: capture close failed"
            self.handle = None

    def metadata(self):
        return {
            "path": self.path.relative_to(self.workspace).as_posix() if self.path else None,
            "encoding": "utf-8",
            "captured_bytes": self.saved,
            "redacted_bytes": self.total,
            "capture_limit_bytes": self.limit,
            "stream_complete": self.complete,
            "capture_truncated": self.saved < self.total or not self.complete,
            "capture_error": self.error,
        }
