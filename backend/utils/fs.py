"""Filesystem helpers shared by tools and runtime-owned projections."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


def atomic_write_text(path: Path | str, text: str, *, encoding: str = "utf-8") -> None:
    """Replace a file atomically, so a failed write never truncates the old copy."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    previous_mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else None
    fd, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as handle:
            handle.write(text)
            handle.flush()
            if previous_mode is not None:
                os.chmod(temporary, previous_mode)
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
