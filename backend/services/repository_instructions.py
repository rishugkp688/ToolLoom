"""Bounded, workspace-scoped discovery; never search the operator's home."""

from backend.services.sandbox.workspace import WorkspaceError

MAX_INSTRUCTION_CHARS = 8_000


def discover(workspace, path=".", *, max_chars=MAX_INSTRUCTION_CHARS):
    target = workspace.resolve(path)
    directory = target if target.is_dir() else target.parent
    relative = directory.relative_to(workspace.root)
    directories = [workspace.root]
    for part in relative.parts:
        directories.append(directories[-1] / part)
    files = []
    remaining = max_chars
    for directory in directories:
        for name in ("AGENTS.md", "CLAUDE.md"):
            relative_path = (directory / name).relative_to(workspace.root).as_posix()
            try:
                candidate = workspace.resolve(relative_path, must_exist=True)
            except WorkspaceError:
                continue
            if not candidate.is_file():
                continue
            try:
                with candidate.open("r", encoding="utf-8", errors="replace") as handle:
                    text = handle.read(max(0, remaining) + 1)
            except OSError as exc:
                files.append({
                    "path": relative_path,
                    "content": f"[instruction file could not be read: {type(exc).__name__}]",
                    "truncated": False,
                })
                continue
            truncated = len(text) > remaining
            content = text[:max(0, remaining)]
            files.append({"path": relative_path, "content": content, "truncated": truncated})
            remaining -= len(content)
    return files
