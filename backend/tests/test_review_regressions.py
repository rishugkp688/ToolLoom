from __future__ import annotations

import asyncio
import json
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.repositories import ToolRepository
from backend.services.context_builder import ContextBuilder
from backend.services.providers.base import ToolCallBlock
from backend.services.providers.fake import FakeProvider
from backend.tests.helpers import enable_demo_add, enable_tool, sandbox_server
from backend.utils.jsonc import loads


@pytest.mark.parametrize("duplicate_in_turn", [True, False])
def test_duplicate_tool_call_ids_never_execute_twice(make_app, duplicate_in_turn):
    from backend.services.providers.base import ModelTurn, Usage

    call = ToolCallBlock("duplicate", "demo__add", {"a": 2, "b": 3})
    turns = [ModelTurn([call, call] if duplicate_in_turn else [call], "tool_use", Usage(), "fake")]
    if not duplicate_in_turn:
        turns.append(ModelTurn([call], "tool_use", Usage(), "fake"))
    with TestClient(
        make_app(provider=FakeProvider(script=turns)), base_url="http://127.0.0.1"
    ) as client:
        enable_demo_add(client)
        cid = client.post("/conversations", json={}).json()["id"]
        response = client.post(f"/conversations/{cid}/messages/stream", json={"message": "add"})
        assert "provider reused a tool-call ID" in response.text
        rows = client.get(f"/conversations/{cid}").json()["messages"]
        assert len([row for row in rows if row["role"] == "tool"]) == (
            0 if duplicate_in_turn else 1
        )
        assert rows[-1]["stop_reason"] == "error"
        assert not client.app.state.active_runs


async def test_stdio_handshake_has_total_deadline():
    from backend.services.mcp_client import MCPError, list_tools

    with pytest.raises(MCPError, match="TimeoutError"):
        await asyncio.wait_for(
            list_tools(
                transport="stdio",
                command=sys.executable,
                args=["-c", "import time; time.sleep(30)"],
                headers={},
                timeout_ms=200,
                verify_ssl=True,
            ),
            timeout=5,
        )


async def test_discovery_reads_all_pages(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from backend.services import mcp_client

    cursors = []

    class Session:
        async def list_tools(self, cursor=None):
            cursors.append(cursor)
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name="first" if cursor is None else "second", description="", inputSchema={}
                    )
                ],
                nextCursor="page2" if cursor is None else None,
            )

    @asynccontextmanager
    async def session(**kwargs):
        yield Session()

    monkeypatch.setattr(mcp_client, "open_session", session)
    tools = await mcp_client.list_tools(
        headers={}, timeout_ms=1000, verify_ssl=True, transport="http"
    )
    assert [tool.name for tool in tools] == ["first", "second"]
    assert cursors == [None, "page2"]


async def test_permission_revoked_during_retry_is_respected(client, monkeypatch):
    server = enable_demo_add(client)
    executor = client.app.state.tool_executor
    calls = 0

    async def fail(*args):
        nonlocal calls
        calls += 1
        client.app.state.tool_registry.update_tool(server["id"], "add", enabled=False)
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(executor.connection_manager, "call_tool", fail)
    result = await executor.execute(ToolCallBlock("retry", "demo__add", {"a": 2, "b": 3}), "retry")
    assert result.is_error and calls == 1
    assert "permissions or connection changed" in result.output_text


def test_jsonc_preserves_strings_and_rejects_duplicate_keys():
    value = loads('{"x": "literal,} and ,] and // text", /* comment */ "y": [1,],}')
    assert value == {"x": "literal,} and ,] and // text", "y": [1]}
    for text in ['{"x":1,"x":2}', '{"x":1} /* never closed']:
        with pytest.raises(ValueError):
            loads(text)


def test_malformed_origin_is_rejected(client):
    assert client.post("/conversations", json={}, headers={"Origin": "http://["}).status_code == 403


def test_missing_mcp_secret_is_a_controlled_connection_failure(client):
    server = client.post(
        "/mcp/servers",
        json={
            "name": "missing-secret",
            "url": "http://127.0.0.1:1/mcp",
            "headers": {"Authorization": "${secrets.not_configured}"},
        },
    ).json()
    for action in ("discover", "test"):
        response = client.post(f"/mcp/servers/{server['id']}/{action}")
        assert response.status_code == 502
        assert "credentials" in response.json()["detail"]


def test_pathological_regex_is_terminated(tmp_path, monkeypatch):
    from backend.services.sandbox import fs_tool
    from backend.services.sandbox.workspace import Workspace, WorkspaceError

    (tmp_path / "input.txt").write_text("a" * 100 + "!")
    monkeypatch.setattr(fs_tool, "GREP_TIMEOUT_SECONDS", 1)
    with pytest.raises(WorkspaceError, match="search time limit exceeded"):
        fs_tool.grep(Workspace(tmp_path), {"pattern": "(a+)+$"})


def test_grep_output_is_bounded(tmp_path):
    from backend.services.sandbox import fs_tool
    from backend.services.sandbox.workspace import Workspace

    (tmp_path / "input.txt").write_text("x" * 400_000)
    result = fs_tool.grep(Workspace(tmp_path), {"pattern": "x"})
    assert len(result) < fs_tool.MAX_RETURN_CHARS + 100
    assert "output limit reached" in result


def test_failed_sync_import_is_atomic(client):
    saved = client.post(
        "/mcp/servers", json={"name": "keep", "url": "http://localhost:9999/mcp"}
    ).json()
    config = {
        "mcpServers": {
            "keep": {"url": "http://localhost:1234/mcp"},
            "broken": {"command": "python"},
        }
    }
    body = client.post("/mcp/servers/import?sync=1", json=config).json()
    assert body["applied"] is False
    assert (
        next(s for s in client.get("/mcp/servers").json() if s["id"] == saved["id"])["url"]
        == saved["url"]
    )


@pytest.mark.parametrize(
    "spec",
    [
        {"url": "file:///etc/passwd"},
        {"url": "https://user:pass@example.com/mcp"},
        {"url": "https://example.com/mcp", "verify_ssl": False},
        {"url": "http://localhost/mcp", "timeout_ms": 1},
        {"url": "http://localhost/mcp", "headers": {"X": "bad\nheader"}},
        {"url": "http://localhost/mcp", "typo": "silently lost"},
        {"url": 42},
        {"url": "http://localhost/mcp", "env": {"TOKEN": "secret"}},
        {"url": "http://localhost/mcp", "type": "http", "transport": "sse"},
    ],
)
def test_invalid_import_does_not_mutate(client, spec):
    result = client.post("/mcp/servers/import", json={"mcpServers": {"bad": spec}}).json()
    assert result["applied"] is False
    assert [s["name"] for s in client.get("/mcp/servers").json()] == ["demo", "sandbox"]


def test_config_view_masks_credentials_and_roundtrips(client):
    client.post(
        "/mcp/servers",
        json={
            "name": "secret-remote",
            "url": "https://example.com/mcp?token=private-query",
            "headers": {"Authorization": "Bearer private-header", "X-Key": "${secrets.example}"},
        },
    )
    current = client.get("/mcp/config").json()
    assert current["redacted"] is True
    assert "private-header" not in json.dumps(current)
    assert "private-query" not in json.dumps(current)
    assert "${secrets.example}" in json.dumps(current)
    result = client.post("/mcp/servers/import", json=current["config"]).json()
    assert result["applied"] is True
    server = client.app.state.tool_registry.get_server_by_name("secret-remote")
    assert "private-query" in server.url and "private-header" in server.headers_json


def test_stdio_json_import_with_explicit_opt_in_and_env(make_app):
    with TestClient(make_app(toolloom_allow_stdio=True), base_url="http://127.0.0.1") as client:
        config = {
            "mcpServers": {
                "local-python": {
                    "command": sys.executable,
                    "args": ["-V"],
                    "env": {"TOKEN": "${secrets.token}"},
                }
            }
        }
        result = client.post("/mcp/servers/import", json=config).json()
        assert result["applied"]
        server = next(s for s in client.get("/mcp/servers").json() if s["name"] == "local-python")
        assert server["enabled"] is False
        exported = client.get("/mcp/config").json()["config"]["mcpServers"]["local-python"]
        assert exported["command"] == sys.executable and exported["env"] == {
            "TOKEN": "${secrets.token}"
        }
        assert client.delete(f"/mcp/servers/{server['id']}").status_code == 204
        assert "local-python" not in client.get("/mcp/config").json()["config"]["mcpServers"]


def test_builtin_config_cannot_be_hijacked(client):
    result = client.post(
        "/mcp/servers/import", json={"servers": {"sandbox": {"url": "http://localhost:9999/mcp"}}}
    ).json()
    assert result["applied"] is False
    assert sandbox_server(client)["transport"] == "local"


def test_changed_connection_revokes_permissions(client):
    registry = client.app.state.tool_registry
    server = registry.create_server(name="remote", url="http://localhost:9999/mcp", enabled=True)
    with client.app.state.db.session() as session:
        repo = ToolRepository(session)
        repo.reconcile(
            server.id, [{"name": "go", "server_name": "remote", "input_schema": {"type": "object"}}]
        )
        repo.update("remote__go", enabled=True, approved=True)
    result = client.post(
        "/mcp/servers/import", json={"servers": {"remote": {"url": "http://localhost:8888/mcp"}}}
    ).json()
    assert result["applied"]
    assert registry.get_server(server.id).enabled is False
    tool = registry.list_tools(server.id)[0]
    assert not tool.enabled and not tool.approved


def test_unchanged_config_does_not_revoke_permissions(client):
    registry = client.app.state.tool_registry
    server = registry.create_server(name="remote", url="http://localhost:9999/mcp", enabled=True)
    result = client.post(
        "/mcp/servers/import", json=client.get("/mcp/config").json()["config"]
    ).json()
    assert result["applied"] and registry.get_server(server.id).enabled


def test_disabled_servers_and_unapproved_tools_not_advertised(client):
    demo = enable_demo_add(client)
    enable_tool(client, "sandbox", "http_fetch")
    client.patch(f"/mcp/servers/{demo['id']}", json={"enabled": False})
    context = ContextBuilder(client.app.state.db, client.app.state.settings).build("conv")
    assert "demo__add" not in [t.name for t in context.tools]
    assert "sandbox__http_fetch" not in [t.name for t in context.tools]


async def test_sandbox_master_switch_blocks_execution_and_preserves_selections(client):
    server = enable_tool(client, "sandbox", "fs_write")
    endpoint = f"/mcp/servers/{server['id']}"
    assert client.patch(endpoint, json={"enabled": False}).status_code == 200
    app = client.app
    context = ContextBuilder(app.state.db, app.state.settings).build("master-switch")
    assert not any(tool.name.startswith("sandbox__") for tool in context.tools)
    call = ToolCallBlock("master", "sandbox__fs_write", {"path": "proof.txt", "content": "ok"})
    result = await app.state.tool_executor.execute(call, "master-switch")
    assert result.is_error and "disabled" in result.output_text
    target = app.state.settings.workspaces_dir / "master-switch" / "proof.txt"
    assert not target.exists()
    assert client.patch(endpoint, json={"enabled": True}).status_code == 200
    tools = client.get(endpoint + "/tools").json()
    assert next(tool for tool in tools if tool["name"] == "fs_write")["enabled"]
    assert not next(tool for tool in tools if tool["name"] == "fs_delete")["enabled"]
    result = await app.state.tool_executor.execute(call, "master-switch")
    assert not result.is_error and target.read_text() == "ok"


def test_changed_tool_schema_revokes_enable_and_approval(client):
    demo = enable_demo_add(client)
    with client.app.state.db.session() as session:
        repo = ToolRepository(session)
        repo.reconcile(
            demo["id"], [{"name": "add", "server_name": "demo", "input_schema": {"type": "object"}}]
        )
        tool = repo.get_by_namespaced("demo__add")
        assert not tool.enabled and not tool.approved


@pytest.mark.parametrize(
    "schema", ["not json", "[]", '{"type":"not-a-type"}', '{"$ref":"https://example.com/schema"}']
)
async def test_bad_tool_schema_fails_closed(client, schema):
    enable_demo_add(client)
    with client.app.state.db.session() as session:
        ToolRepository(session).get_by_namespaced("demo__add").input_schema_json = schema
    result = await client.app.state.tool_executor.execute(
        ToolCallBlock("bad", "demo__add", {"a": 2, "b": 3}), "conv"
    )
    assert result.is_error and "schema" in result.output_text


async def test_spill_path_cannot_escape_and_secrets_are_redacted(client):
    enable_tool(client, "sandbox", "fs_write")
    enable_tool(client, "sandbox", "fs_read")
    client.post("/secrets", json={"name": "private", "value": "credential-to-hide"})
    executor = client.app.state.tool_executor
    await executor.execute(
        ToolCallBlock(
            "write",
            "sandbox__fs_write",
            {"path": "big.txt", "content": "credential-to-hide" + "x" * 50000},
        ),
        "spill",
    )
    result = await executor.execute(
        ToolCallBlock("../../outside", "sandbox__fs_read", {"path": "big.txt"}), "spill"
    )
    assert result.truncated and "credential-to-hide" not in result.output_text
    root = client.app.state.settings.workspaces_dir / "spill"
    assert not (root.parent / "outside.txt").exists()
    files = list((root / ".transcripts").glob("*.txt"))
    assert len(files) == 1 and "credential-to-hide" not in files[0].read_text()


def test_workspace_status_rejects_external_symlink(client, tmp_path):
    from backend.services.sandbox.workspace import workspace_for

    workspace = workspace_for(client.app.state.settings.workspaces_dir, "safe")
    secret = tmp_path / "outside.txt"
    secret.write_text("PRIVATE STATUS")
    try:
        (workspace.root / "notes.md").symlink_to(secret)
    except OSError:
        pytest.skip("symlinks unavailable")
    built = ContextBuilder(client.app.state.db, client.app.state.settings).build("safe")
    assert "PRIVATE STATUS" not in built.system


def test_bad_zip_and_invalid_replacement_preserve_installed_skill(client):
    raw = b"---\nname: keep-me\ndescription: Original skill\n---\nKeep this."
    assert client.post("/skills/upload", files={"file": ("keep-me.md", raw)}).status_code == 201
    assert (
        client.post("/skills/upload", files={"file": ("bad.zip", b"not a zip")}).status_code == 400
    )
    oversized = b"---\nname: keep-me\ndescription: " + b"x" * 1100 + b"\n---\nBad replacement"
    assert (
        client.post("/skills/upload", files={"file": ("keep-me.md", oversized)}).status_code == 400
    )
    assert (client.app.state.settings.data_dir / "skills/keep-me/SKILL.md").read_bytes() == raw


class SlowProvider(FakeProvider):
    async def stream(self, **kwargs):
        await asyncio.sleep(0.15)
        async for event in super().stream(**kwargs):
            yield event


async def test_concurrent_turn_and_delete_are_rejected_before_persist(make_app):
    app = make_app(provider=SlowProvider())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        cid = (await client.post("/conversations", json={})).json()["id"]
        first = asyncio.create_task(
            client.post(f"/conversations/{cid}/messages", json={"message": "first"})
        )
        while cid not in app.state.active_runs:
            await asyncio.sleep(0.001)
        overlap = await client.post(f"/conversations/{cid}/messages", json={"message": "overlap"})
        assert overlap.status_code == 409
        assert (await client.delete(f"/conversations/{cid}")).status_code == 409
        assert (await first).status_code == 200
        assert not app.state.active_runs
        detail = (await client.get(f"/conversations/{cid}")).json()
        assert [m["content"] for m in detail["messages"]] == ["first", "Echo: first"]
        assert (await client.delete(f"/conversations/{cid}")).status_code == 204


async def test_cancelled_run_releases_reservation(make_app):
    app = make_app(provider=SlowProvider())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        cid = (await client.post("/conversations", json={})).json()["id"]
        pending = asyncio.create_task(
            client.post(f"/conversations/{cid}/messages", json={"message": "cancel"})
        )
        while cid not in app.state.active_runs:
            await asyncio.sleep(0.001)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not app.state.active_runs
        assert (
            await client.post(f"/conversations/{cid}/messages", json={"message": "retry"})
        ).status_code == 200


def test_foreign_host_read_is_rejected(client):
    assert client.get("/mcp/config", headers={"host": "evil.example"}).status_code == 403
