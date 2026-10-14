"""User file uploads into a conversation workspace."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from backend.services.sandbox.workspace import Workspace, WorkspaceError

ALLOWED_EXTENSIONS = {".txt", ".md", ".markdown", ".py", ".json", ".csv"}
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
INLINE_LIMIT_BYTES = 20 * 1024
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class UploadError(ValueError):
    """Rejected upload."""


@dataclass
class SavedUpload:
    name: str
    path: str
    size_bytes: int
    inlined: bool
    preview: str


def _safe_filename(filename: str) -> str:
    raw = (filename or "").replace("\\", "/").split("/")[-1].strip()
    if not raw or raw in {".", ".."}:
        raise UploadError("invalid filename")
    if re.match(r"^[A-Za-z]:", raw) or raw.startswith("."):
        raise UploadError("invalid filename")
    cleaned = re.sub(r"[^A-Za-z0-9._ -]", "_", raw).strip() or "upload"
    if Path(cleaned).stem.upper() in _WINDOWS_RESERVED:
        cleaned = f"upload_{cleaned}"
    return cleaned[:120]


def save_upload(workspace: Workspace, filename: str, raw: bytes) -> SavedUpload:
    safe = _safe_filename(filename)
    extension = Path(safe).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise UploadError(
            f"unsupported file type {extension or '(none)'}; allowed: "
            + ", ".join(sorted(ALLOWED_EXTENSIONS))
        )
    if len(raw) > MAX_UPLOAD_BYTES:
        raise UploadError(f"file exceeds the {MAX_UPLOAD_BYTES // 1024} KB limit")
    if not raw:
        raise UploadError("empty file")
    if b"\x00" in raw[:4096]:
        raise UploadError("binary content is not allowed")

    target = workspace.resolve(safe)
    counter = 1
    while target.exists():
        target = workspace.resolve(f"{Path(safe).stem} ({counter}){extension}")
        counter += 1
    target.write_bytes(raw)

    text = raw.decode("utf-8", "replace")
    inlined = len(raw) <= INLINE_LIMIT_BYTES
    preview = text if inlined else text[:INLINE_LIMIT_BYTES]
    return SavedUpload(
        name=target.name,
        path=workspace.relative(target),
        size_bytes=len(raw),
        inlined=inlined,
        preview=preview,
    )


def build_attachment_preview(uploads: list[SavedUpload]) -> str:
    blocks: list[str] = []
    for upload in uploads:
        body = upload.preview
        if not upload.inlined:
            body += f"\n…[preview truncated; call fs_read on `{upload.path}` for the rest]"
        blocks.append(
            f'<attachment name="{upload.name}" path="{upload.path}" '
            f'size_bytes="{upload.size_bytes}" inlined="{str(upload.inlined).lower()}">\n'
            f"{body}\n</attachment>"
        )
    return "\n".join(blocks)


def attachment_metadata(uploads: list[SavedUpload]) -> list[dict]:
    return [
        {"name": item.name, "path": item.path, "size_bytes": item.size_bytes} for item in uploads
    ]


_LEGACY_HEADER = re.compile(
    r'^<attachment name="([^"\n]+)" path="([^"\n]+)" '
    r'size_bytes="(\d{1,12})" inlined="(true|false)">\n'
)


def legacy_attachment_presentation(content: str) -> tuple[str, list[dict]]:
    """Read old generated wrappers without rewriting the stored transcript.

    New messages use explicit metadata. For legacy inlined files, the recorded
    byte count locates the closing wrapper even if the file contains that tag.
    Unrecognized/malformed text stays visible rather than being silently removed.
    """
    remaining = content
    files = []
    while len(files) < 10 and (header := _LEGACY_HEADER.match(remaining)):
        name, path, size, inlined = header.groups()
        if path.startswith(("/", "\\")) or ".." in path.replace("\\", "/").split("/"):
            return content, []
        body = remaining[header.end() :]
        if inlined == "true":
            raw = body.encode("utf-8")
            count = int(size)
            if not raw[count:].startswith(b"\n</attachment>"):
                return content, []
            try:
                tail = raw[count + len(b"\n</attachment>") :].decode("utf-8")
            except UnicodeDecodeError:
                return content, []
        else:
            footer = f"\n…[preview truncated; call fs_read on `{path}` for the rest]\n</attachment>"
            index = body.find(footer)
            if index < 0:
                return content, []
            tail = body[index + len(footer) :]
        files.append({"name": name, "path": path, "size_bytes": int(size)})
        if not tail:
            return "", files
        if tail.startswith("\n\n"):
            return tail[2:], files
        if not tail.startswith("\n<attachment "):
            return content, []
        remaining = tail[1:]
    return content, []


def resolve_attachment(workspace: Workspace, rel_path: str) -> Path:
    try:
        target = workspace.resolve(rel_path, must_exist=True)
    except WorkspaceError as exc:
        raise UploadError(str(exc)) from exc
    if not target.is_file():
        raise UploadError(f"not a file: {rel_path}")
    return target


def load_saved_upload(workspace: Workspace, rel_path: str) -> SavedUpload:
    """Re-read an already-uploaded workspace file into an attachment record."""
    target = resolve_attachment(workspace, rel_path)
    with target.open("rb") as handle:
        raw = handle.read(INLINE_LIMIT_BYTES)
    text = raw.decode("utf-8", "replace")
    size = target.stat().st_size
    inlined = size <= INLINE_LIMIT_BYTES
    return SavedUpload(
        name=target.name,
        path=workspace.relative(target),
        size_bytes=size,
        inlined=inlined,
        preview=text if inlined else text[:INLINE_LIMIT_BYTES],
    )
