from __future__ import annotations

import pytest

from backend.services.sandbox import fs_tool
from backend.services.sandbox.workspace import Workspace, WorkspaceError

BAD_PATHS = [
    "/etc/passwd",
    "//server/share/file",
    "\\\\server\\share\\file",
    "C:\\Windows\\system32\\config",
    "~/secrets.txt",
    "../outside.txt",
    "nested/../../escape.txt",
    "with\x00nul",
]


@pytest.mark.parametrize("bad", BAD_PATHS)
def test_resolve_rejects_unsafe_paths(tmp_path, bad):
    workspace = Workspace(tmp_path / "ws")
    with pytest.raises(WorkspaceError):
        workspace.resolve(bad)


def test_symlink_escape_is_rejected(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = workspace.root / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks not supported on this platform")
    with pytest.raises(WorkspaceError):
        workspace.resolve("link.txt")


def test_write_read_roundtrip(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    assert "wrote" in fs_tool.write(workspace, {"path": "notes/a.txt", "content": "hello"})
    assert fs_tool.read(workspace, {"path": "notes/a.txt"}) == "hello"


def test_append_mode(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    fs_tool.write(workspace, {"path": "log.txt", "content": "one\n"})
    fs_tool.write(workspace, {"path": "log.txt", "content": "two\n", "mode": "append"})
    assert fs_tool.read(workspace, {"path": "log.txt"}) == "one\ntwo\n"


def test_read_paging(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    fs_tool.write(workspace, {"path": "lines.txt", "content": "\n".join(f"line{i}" for i in range(10))})
    assert fs_tool.read(workspace, {"path": "lines.txt", "offset": 3, "limit": 2}) == "line2\nline3"


def test_binary_read_is_refused(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    (workspace.root / "blob.bin").write_bytes(b"\x00\x01\x02")
    with pytest.raises(WorkspaceError):
        fs_tool.read(workspace, {"path": "blob.bin"})


def test_listing_hides_scratch_dirs(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    fs_tool.write(workspace, {"path": "visible.txt", "content": "x"})
    (workspace.transcripts_dir() / "t.txt").write_text("x", encoding="utf-8")
    listing = fs_tool.list_dir(workspace, {})
    assert "visible.txt" in listing
    assert "transcripts" not in listing


def test_grep_finds_matches(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    fs_tool.write(workspace, {"path": "a.txt", "content": "alpha\nbeta\n"})
    fs_tool.write(workspace, {"path": "b.txt", "content": "gamma\n"})
    result = fs_tool.grep(workspace, {"pattern": "beta"})
    assert "a.txt" in result and "beta" in result


def test_delete_refuses_workspace_root(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    with pytest.raises(WorkspaceError):
        fs_tool.delete(workspace, {"path": "."})


def test_delete_nonempty_dir_requires_recursive(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    fs_tool.write(workspace, {"path": "dir/one.txt", "content": "x"})
    with pytest.raises(WorkspaceError):
        fs_tool.delete(workspace, {"path": "dir"})
    assert "deleted" in fs_tool.delete(workspace, {"path": "dir", "recursive": True})
