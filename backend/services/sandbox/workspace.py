"""Per-conversation workspace with strict path containment.

Every filesystem tool resolves paths through :meth:`Workspace.resolve`, which
rejects absolute paths, drive letters, UNC paths, ``~`` and anything that escapes
the workspace root after symlink resolution. A model-run ``os.symlink`` cannot be
used to reach outside the workspace.
"""

from __future__ import annotations

import re
from pathlib import Path

_DRIVE = re.compile(r"^[A-Za-z]:")
# Runtime scratch directories that are never listed or downloadable.
HIDDEN_DIRS = {".transcripts", ".vendor", ".cache", ".tmp"}


class WorkspaceError(RuntimeError):
    """Raised for any rejected or invalid workspace path."""


class Workspace:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # -- path safety ----------------------------------------------------------

    def resolve(self, user_path: str | None, *, must_exist: bool = False) -> Path:
        raw = ("" if user_path is None else str(user_path)).strip()
        if raw in {"", "."}:
            return self.root
        if "\x00" in raw:
            raise WorkspaceError("invalid path")
        if _DRIVE.match(raw):
            raise WorkspaceError("drive-letter paths are not allowed")
        if raw.startswith(("\\\\", "//")):
            raise WorkspaceError("UNC paths are not allowed")
        if raw.startswith("~"):
            raise WorkspaceError("home-directory paths are not allowed")

        candidate = Path(raw)
        if candidate.is_absolute():
            raise WorkspaceError("absolute paths are not allowed")

        resolved = (self.root / candidate).resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise WorkspaceError("path escapes the workspace")
        if must_exist and not resolved.exists():
            raise WorkspaceError(f"no such path: {raw}")
        return resolved

    def relative(self, path: Path) -> str:
        rel = path.resolve().relative_to(self.root).as_posix()
        return rel if rel != "." else "."

    def is_hidden(self, path: Path) -> bool:
        try:
            rel = path.resolve().relative_to(self.root)
        except ValueError:
            return True
        return any(part in HIDDEN_DIRS for part in rel.parts)

    def transcripts_dir(self) -> Path:
        directory = self.root / ".transcripts"
        directory.mkdir(parents=True, exist_ok=True)
        return directory


def workspace_for(workspaces_dir: Path, conversation_id: str) -> Workspace:
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", conversation_id)[:64] or "default"
    return Workspace(Path(workspaces_dir) / safe_id)
