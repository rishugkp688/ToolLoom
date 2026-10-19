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
# Runtime-owned namespace. Projections such as the task plan live here, so an
# ordinary user file called plan.md or request.md is never overwritten.
RUNTIME_DIR = ".toolloom"
# Runtime scratch directories that are never listed or downloadable.
HIDDEN_DIRS = {RUNTIME_DIR, ".transcripts", ".vendor", ".cache", ".tmp"}
# Dependency/build trees are pruned from listing, search and download traversal.
# They remain readable by explicit path, but walking them wastes the result budget.
SKIP_TREE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    "dist",
    "build",
    "target",
    ".next",
    ".nuxt",
    "coverage",
    ".terraform",
}
# Build outputs are deliverables, even though search/revision walks skip them.
EXPORT_SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__"}


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

    def iter_files(self, root: Path | None = None, *, for_export: bool = False):
        """Yield visible files, pruning dependency/build trees and runtime scratch."""
        import os

        base = Path(root) if root is not None else self.root
        skip = EXPORT_SKIP_DIRS if for_export else SKIP_TREE_DIRS
        for directory, dirnames, filenames in os.walk(base):
            current = Path(directory)
            dirnames[:] = sorted(
                name for name in dirnames if name not in skip and not self.is_hidden(current / name)
            )
            for name in sorted(filenames):
                path = current / name
                if not self.is_hidden(path):
                    yield path

    def transcripts_dir(self) -> Path:
        directory = self.root / ".transcripts"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def runtime_path(self, name: str) -> Path:
        """A path inside the runtime-owned namespace, never a user document."""
        directory = self.root / RUNTIME_DIR
        directory.mkdir(parents=True, exist_ok=True)
        return directory / name

    def revision(self, max_files: int = 4_000) -> str:
        """A cheap fingerprint of visible workspace state.

        It distinguishes "the same command run again" from "the same command run
        after a file changed", which is what a duplicate-call guard needs to know.
        Bounded so a very large tree cannot turn every tool call into a full walk.
        """
        import hashlib

        digest = hashlib.sha256()
        count = 0
        # Builds can change what verification reads; include deliverables while
        # still pruning dependency/VCS trees. A capped walk is explicitly partial.
        for path in self.iter_files(for_export=True):
            try:
                stat = path.stat()
            except OSError:
                continue
            digest.update(f"{self.relative(path)}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode())
            count += 1
            if count >= max_files:
                return "partial:" + digest.hexdigest()
        return digest.hexdigest()


def workspace_for(workspaces_dir: Path, conversation_id: str) -> Workspace:
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", conversation_id)[:64] or "default"
    return Workspace(Path(workspaces_dir) / safe_id)
