"""Workspace-scoped filesystem tools (fs_read / fs_write / fs_list / fs_grep / fs_delete)."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from backend.services.sandbox.workspace import HIDDEN_DIRS, Workspace, WorkspaceError

MAX_UNPAGED_READ = 10 * 1024 * 1024
MAX_PAGED_READ = 200 * 1024 * 1024
MAX_RETURN_CHARS = 200_000
MAX_WRITE_BYTES = 10 * 1024 * 1024
MAX_LIST_ENTRIES = 500
MAX_GREP_MATCHES = 200
GREP_TIMEOUT_SECONDS = 5
_BINARY_SNIFF = 4096


def _is_binary(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return b"\x00" in handle.read(_BINARY_SNIFF)
    except OSError:
        return False


def read(workspace: Workspace, arguments: dict) -> str:
    target = workspace.resolve(arguments.get("path"), must_exist=True)
    if target.is_dir():
        raise WorkspaceError(f"{workspace.relative(target)} is a directory; use fs_list")

    size = target.stat().st_size
    offset = arguments.get("offset")
    limit = arguments.get("limit")
    paged = offset is not None or limit is not None

    if paged:
        if size > MAX_PAGED_READ:
            raise WorkspaceError(f"file too large to page ({size} bytes)")
        if _is_binary(target):
            raise WorkspaceError("refusing to read a binary file")
        start = max(0, int(offset or 1)) - 1
        count = int(limit) if limit else 500
        lines: list[str] = []
        with target.open("r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index < start:
                    continue
                if len(lines) >= count:
                    break
                lines.append(line.rstrip("\n"))
        return "\n".join(lines) if lines else "(no lines in range)"

    if size > MAX_UNPAGED_READ:
        raise WorkspaceError(f"file is {size} bytes; re-read with `offset`/`limit` (line paging)")
    if _is_binary(target):
        raise WorkspaceError("refusing to read a binary file")

    text = target.read_text(encoding="utf-8", errors="replace")
    if len(text) > MAX_RETURN_CHARS:
        text = text[:MAX_RETURN_CHARS] + f"\n…[truncated at {MAX_RETURN_CHARS} chars]"
    return text


def write(workspace: Workspace, arguments: dict) -> str:
    target = workspace.resolve(arguments.get("path"))
    content = arguments.get("content")
    if content is None:
        raise WorkspaceError("`content` is required")
    data = str(content)
    encoded = data.encode("utf-8")
    if len(encoded) > MAX_WRITE_BYTES:
        raise WorkspaceError(f"refusing to write {len(encoded)} bytes (max {MAX_WRITE_BYTES})")
    if target == workspace.root or target.is_dir():
        raise WorkspaceError("cannot write to a directory")
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = arguments.get("mode") or "write"
    if mode not in {"write", "append"}:
        raise WorkspaceError("`mode` must be 'write' or 'append'")
    with target.open("a" if mode == "append" else "w", encoding="utf-8") as handle:
        handle.write(data)
    return f"wrote {len(encoded)} bytes to {workspace.relative(target)}"


def list_dir(workspace: Workspace, arguments: dict) -> str:
    target = workspace.resolve(arguments.get("path") or ".")
    if not target.is_dir():
        raise WorkspaceError(f"{workspace.relative(target)} is not a directory")
    entries: list[tuple[str, bool]] = []
    for child in target.iterdir():
        if child.name in HIDDEN_DIRS:
            continue
        if workspace.is_hidden(child):
            continue
        entries.append((child.name, child.is_dir()))
        if len(entries) >= MAX_LIST_ENTRIES:
            break
    entries.sort(key=lambda item: (not item[1], item[0].lower()))
    lines = [f"{name}/" if is_dir else name for name, is_dir in entries]
    body = "\n".join(lines) if lines else "(empty)"
    return f"{workspace.relative(target)}\n{body}"


def grep(workspace: Workspace, arguments: dict) -> str:
    # A pathological regex can hold Python's GIL indefinitely. A thread timeout
    # cannot interrupt it; run this fixed search implementation in a killable worker.
    try:
        result = subprocess.run(
            [sys.executable, "-I", str(Path(__file__).with_name("grep_worker.py"))],
            input=json.dumps({"root": str(workspace.root), "arguments": arguments}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=GREP_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceError(
            "search time limit exceeded; simplify the regex or narrow the path"
        ) from exc
    if result.returncode:
        raise WorkspaceError("search worker failed")
    data = json.loads(result.stdout)
    if "error" in data:
        raise WorkspaceError(data["error"])
    return data["result"]


def _grep(workspace: Workspace, arguments: dict) -> str:
    pattern = arguments.get("pattern")
    if not pattern:
        raise WorkspaceError("`pattern` is required")
    flags = re.IGNORECASE if arguments.get("ignore_case", True) else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        raise WorkspaceError(f"invalid regex: {exc}") from exc

    root = workspace.resolve(arguments.get("path") or ".")
    if root.is_file():
        files = [root]
    else:
        files = [
            path
            for path in sorted(root.rglob("*"))
            if path.is_file() and not workspace.is_hidden(path)
        ]

    matches: list[str] = []
    chars = 0
    for path in files:
        if _is_binary(path):
            continue
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for number, line in enumerate(handle, start=1):
                    if regex.search(line):
                        match = f"{workspace.relative(path)}:{number}: {line.rstrip()}"
                        matches.append(match[: max(0, MAX_RETURN_CHARS - chars)])
                        chars += len(match) + 1
                        if chars >= MAX_RETURN_CHARS:
                            matches.append("…[output limit reached; narrow the search]")
                            return "\n".join(matches)
                        if len(matches) >= MAX_GREP_MATCHES:
                            matches.append("…[more matches omitted]")
                            return "\n".join(matches)
        except OSError:
            continue
    return "\n".join(matches) if matches else "(no matches)"


def delete(workspace: Workspace, arguments: dict) -> str:
    target = workspace.resolve(arguments.get("path"), must_exist=True)
    if target == workspace.root:
        raise WorkspaceError("refusing to delete the workspace root")
    recursive = bool(arguments.get("recursive", False))
    if target.is_dir():
        if not recursive:
            entries = [p.name for p in target.iterdir() if p.name not in HIDDEN_DIRS]
            if entries:
                raise WorkspaceError("directory is not empty; pass recursive=true to delete")
        import shutil

        shutil.rmtree(target)
    else:
        target.unlink()
    return f"deleted {workspace.relative(target)}"
