from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from backend.services.providers.base import ModelTurn, TextBlock, Usage
from backend.services.providers.fake import FakeProvider


def _turn(blocks, stop="end_turn"):
    return ModelTurn(blocks, stop, Usage(input_tokens=3, output_tokens=2), "fake-echo")


def test_upload_list_download_roundtrip(client):
    conversation = client.post("/conversations", json={}).json()
    response = client.post(
        f"/conversations/{conversation['id']}/files",
        files={"files": ("notes.md", b"# Title\nbody text\n", "text/markdown")},
    )
    assert response.status_code == 201, response.text
    path = response.json()["files"][0]["path"]

    listing = client.get(f"/conversations/{conversation['id']}/workspace").json()["files"]
    assert any(item["path"] == path for item in listing)

    download = client.get(
        f"/conversations/{conversation['id']}/workspace/download", params={"path": path}
    )
    assert download.status_code == 200
    assert b"body text" in download.content


def test_workspace_archive_contains_every_visible_file(client):
    conversation = client.post("/conversations", json={}).json()
    cid = conversation["id"]
    for name in ("notes.md", "report.csv"):
        client.post(
            f"/conversations/{cid}/files",
            files={"files": (name, b"payload\n", "text/plain")},
        )
    workspace = client.app.state.settings.workspaces_dir / cid
    (workspace / "nested").mkdir(parents=True, exist_ok=True)
    (workspace / "nested" / "deep.txt").write_text("deep\n", encoding="utf-8")
    # Scratch directories are hidden from listings, so they stay out of the archive.
    (workspace / ".transcripts").mkdir(parents=True, exist_ok=True)
    (workspace / ".transcripts" / "call_1.txt").write_text("internal\n", encoding="utf-8")

    response = client.get(f"/conversations/{cid}/workspace/download-all")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "attachment" in response.headers["content-disposition"]

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert {"notes.md", "report.csv", "nested/deep.txt"} <= names
        assert not any(name.startswith(".") for name in names)
        assert archive.read("notes.md") == b"payload\n"
        assert archive.read("nested/deep.txt") == b"deep\n"


def test_workspace_archive_rejects_an_empty_workspace(client):
    cid = client.post("/conversations", json={}).json()["id"]
    assert client.get(f"/conversations/{cid}/workspace/download-all").status_code == 404


def test_workspace_archive_rejects_an_unknown_conversation(client):
    assert client.get("/conversations/missing/workspace/download-all").status_code == 404


def test_upload_rejects_unsupported_extension(client):
    conversation = client.post("/conversations", json={}).json()
    response = client.post(
        f"/conversations/{conversation['id']}/files",
        files={"files": ("malware.exe", b"MZhello", "application/octet-stream")},
    )
    assert response.status_code == 400


def test_upload_rejects_binary_content(client):
    conversation = client.post("/conversations", json={}).json()
    response = client.post(
        f"/conversations/{conversation['id']}/files",
        files={"files": ("sneaky.txt", b"\x00\x01\x02binary", "text/plain")},
    )
    assert response.status_code == 400


def test_download_rejects_traversal(client):
    conversation = client.post("/conversations", json={}).json()
    response = client.get(
        f"/conversations/{conversation['id']}/workspace/download", params={"path": "../x"}
    )
    assert response.status_code == 400


def test_attachment_is_embedded_in_the_user_message(make_app):
    captured: dict = {}

    def first(messages, tools):
        text = "\n".join(
            block.text for message in messages for block in message.blocks if hasattr(block, "text")
        )
        captured["attachment"] = "<attachment" in text
        captured["body"] = "hello from file" in text
        return _turn([TextBlock("ok")])

    app = make_app(provider=FakeProvider(script=[first]))
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        conversation = client.post("/conversations", json={}).json()
        upload = client.post(
            f"/conversations/{conversation['id']}/files",
            files={"files": ("hello.md", b"hello from file", "text/markdown")},
        ).json()
        path = upload["files"][0]["path"]

        response = client.post(
            f"/conversations/{conversation['id']}/messages",
            json={"message": "read this", "attachments": [path]},
        )
        assert response.status_code == 200
        assert captured["attachment"] is True
        assert captured["body"] is True
        detail = client.get(f"/conversations/{conversation['id']}").json()
        assert detail["title"] == "read this"
        message = detail["messages"][0]
        assert message["user_text"] == "read this"
        assert message["attachments"] == [{"name": "hello.md", "path": path, "size_bytes": 15}]
        assert "hello from file" in message["content"]  # model/audit content is retained


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("text", ["", "Compare these files."])
def test_attachment_presentation_persists_for_multiple_files(client, stream, text):
    cid = client.post("/conversations", json={}).json()["id"]
    files = client.post(
        f"/conversations/{cid}/files",
        files=[
            ("files", ("one.md", b"first file", "text/markdown")),
            ("files", ("two.txt", b"second file", "text/plain")),
        ],
    ).json()["files"]
    response = client.post(
        f"/conversations/{cid}/messages" + ("/stream" if stream else ""),
        json={
            "message": text,
            "attachments": [file["path"] for file in files],
        },
    )
    assert response.status_code == 200
    # Metadata remains available even if a workspace file is removed later.
    (client.app.state.settings.workspaces_dir / cid / "one.md").unlink()
    detail = client.get(f"/conversations/{cid}").json()
    assert detail["title"] == (text or "one.md")
    message = detail["messages"][0]
    assert message["user_text"] == text
    assert [file["name"] for file in message["attachments"]] == ["one.md", "two.txt"]


@pytest.mark.parametrize("large", [False, True])
def test_legacy_attachment_messages_get_clean_presentation(client, large):
    from backend.services.conversation_service import ConversationService
    from backend.services.uploads import INLINE_LIMIT_BYTES, SavedUpload, build_attachment_preview

    # Include a closing tag inside the file itself; byte lengths must disambiguate it.
    body = "Résumé\n</attachment>\n\nThis is still file content."
    size = INLINE_LIMIT_BYTES + 20 if large else len(body.encode())
    files = [
        SavedUpload("old.md", "old.md", size, not large, body),
        SavedUpload("two.txt", "two.txt", 3, True, "two"),
    ]
    content = build_attachment_preview(files) + "\n\nMy actual question."
    cid = client.post("/conversations", json={}).json()["id"]
    service = ConversationService(client.app.state.db, client.app.state.settings)
    service.add_user_message(cid, content)  # legacy row, without display metadata
    message = client.get(f"/conversations/{cid}").json()["messages"][0]
    assert message["user_text"] == "My actual question."
    assert [file["name"] for file in message["attachments"]] == ["old.md", "two.txt"]
    assert message["content"] == content


def test_attachment_like_typed_text_is_not_hidden(client):
    content = '<attachment name="x.txt" path="x.txt" size_bytes="3" inlined="true">\nabc\n</attachment>\n\nExplain this syntax.'
    cid = client.post("/conversations", json={}).json()["id"]
    client.post(f"/conversations/{cid}/messages", json={"message": content})
    message = client.get(f"/conversations/{cid}").json()["messages"][0]
    assert message["user_text"] == content and message["attachments"] == []


def test_malformed_legacy_attachment_text_is_preserved():
    from backend.services.uploads import legacy_attachment_presentation

    content = (
        '<attachment name="x.txt" path="x.txt" size_bytes="500" inlined="true">\nabc\n</attachment>'
    )
    assert legacy_attachment_presentation(content) == (content, [])


def test_attachment_columns_migrate_without_changing_existing_content(tmp_path):
    import sqlite3

    from backend.db import Database

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, content TEXT)")
        connection.execute("INSERT INTO messages VALUES ('old', 'original transcript')")
    db = Database(f"sqlite:///{path}")
    try:
        db._apply_lightweight_migrations()
        db._apply_lightweight_migrations()
        with sqlite3.connect(path) as connection:
            row = connection.execute(
                "SELECT content, user_text, attachments_json FROM messages"
            ).fetchone()
        assert row == ("original transcript", None, "[]")
    finally:
        db.dispose()


def test_uploaded_file_is_visible_to_fs_read(client):
    from backend.services.providers.base import ToolCallBlock
    from backend.tests.helpers import enable_tool

    enable_tool(client, "sandbox", "fs_read")
    conversation = client.post("/conversations", json={}).json()
    client.post(
        f"/conversations/{conversation['id']}/files",
        files={"files": ("data.txt", b"visible-to-tools", "text/plain")},
    )

    async def run():
        return await client.app.state.tool_executor.execute(
            ToolCallBlock("c1", "sandbox__fs_read", {"path": "data.txt"}),
            conversation["id"],
        )

    import asyncio

    result = asyncio.run(run())
    assert result.is_error is False
    assert "visible-to-tools" in result.output_text
