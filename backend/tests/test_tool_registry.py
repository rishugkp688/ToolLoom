from __future__ import annotations

from fastapi.testclient import TestClient

from backend.tests.helpers import demo_server, enable_demo_add, sandbox_server


def test_seed_ships_only_runtime_owned_servers(make_app):
    """A normal installation ships the sandbox and the generated-tool host."""
    with TestClient(make_app(toolloom_seed_demo=False), base_url="http://127.0.0.1:8000") as client:
        assert [s["name"] for s in client.get("/mcp/servers").json()] == ["generated", "sandbox"]


def test_seed_creates_demo_server_with_disabled_tools(client):
    server = demo_server(client)
    assert server["transport"] == "local"
    assert server["enabled"] is True

    tools = client.get(f"/mcp/servers/{server['id']}/tools").json()
    add = next(t for t in tools if t["name"] == "add")
    assert add["namespaced_name"] == "demo__add"
    assert add["enabled"] is False  # default-deny
    assert add["input_schema"]["required"] == ["a", "b"]


def test_enabling_a_tool_survives_rediscovery(client):
    server = enable_demo_add(client)

    response = client.post(f"/mcp/servers/{server['id']}/discover")
    assert response.status_code == 200
    body = response.json()
    assert body["added"] == 0
    assert body["removed"] == 0

    tools = client.get(f"/mcp/servers/{server['id']}/tools").json()
    add = next(t for t in tools if t["name"] == "add")
    assert add["enabled"] is True  # operator toggle preserved


def test_rediscovery_does_not_reenable_and_prunes_removed(client):
    server = enable_demo_add(client)
    app = client.app
    connection_manager = app.state.mcp_connection_manager
    original = connection_manager.discover_tools

    # Simulate a server that no longer offers any tools.
    async def no_tools(_server):
        return []

    connection_manager.discover_tools = no_tools
    try:
        body = client.post(f"/mcp/servers/{server['id']}/discover").json()
        assert body["removed"] == 1
        assert client.get(f"/mcp/servers/{server['id']}/tools").json() == []
    finally:
        connection_manager.discover_tools = original

    # Re-discovery re-adds the tool but keeps it disabled (never silently enabled).
    body = client.post(f"/mcp/servers/{server['id']}/discover").json()
    assert body["added"] == 1
    tools = client.get(f"/mcp/servers/{server['id']}/tools").json()
    assert tools[0]["enabled"] is False


def test_test_endpoint_reports_tool_count(client):
    server = demo_server(client)
    body = client.post(f"/mcp/servers/{server['id']}/test").json()
    assert body["ok"] is True
    assert body["tool_count"] == 1


def test_import_jsonc_config(client):
    config = """
    {
      // a comment and a trailing comma
      "servers": {
        "remote-one": {
          "url": "http://localhost:9999/mcp",
          "type": "http",
          "headers": { "Authorization": "Bearer ${secrets.token}" },
        },
      },
    }
    """
    response = client.post(
        "/mcp/servers/import", content=config, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 200
    results = response.json()["results"]
    assert results[0]["name"] == "remote-one"
    assert results[0]["ok"] is True
    assert results[0]["transport"] == "http"

    names = [s["name"] for s in client.get("/mcp/servers").json()]
    assert "remote-one" in names


def test_import_rejects_stdio_command_server(client):
    config = '{"servers": {"local-thing": {"command": "echo", "args": ["hi"]}}}'
    body = client.post("/mcp/servers/import", content=config).json()
    assert body["results"][0]["ok"] is False
    assert "TOOLLOOM_ALLOW_STDIO" in body["results"][0]["error"]


def test_import_sync_deletes_unlisted_remote_but_not_local(client):
    client.post("/mcp/servers", json={"name": "temp-remote", "transport": "http", "url": "http://x/mcp"})
    body = client.post("/mcp/servers/import?sync=1", content='{"servers": {}}').json()
    assert body["results"] == []
    names = [s["name"] for s in client.get("/mcp/servers").json()]
    assert "temp-remote" not in names
    # Sync removal never touches in-process servers: the built-in and the
    # seeded fixture both survive.
    assert "sandbox" in names
    assert "demo" in names


def test_create_server_rejects_reserved_name(client):
    response = client.post(
        "/mcp/servers", json={"name": "bad__name", "transport": "http", "url": "http://x/mcp"}
    )
    assert response.status_code == 400


def test_builtin_disable_survives_restart(make_app):
    """Built-ins cannot be removed, and explicit disablement survives restart."""
    import tempfile
    from pathlib import Path

    from fastapi.testclient import TestClient

    from backend.config import Settings
    from backend.main import create_app
    from backend.services.providers.fake import FakeProvider

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp) / "data"

        def build():
            settings = Settings(_env_file=None, llm_provider="fake", toolloom_data_dir=data_dir)
            return create_app(settings=settings, provider=FakeProvider())

        with TestClient(build(), base_url="http://127.0.0.1:8000") as first:
            server = sandbox_server(first)
            assert first.delete(f"/mcp/servers/{server['id']}").status_code == 400
            first.patch(f"/mcp/servers/{server['id']}", json={"enabled": False})
            assert sandbox_server(first)["enabled"] is False

        with TestClient(build(), base_url="http://127.0.0.1:8000") as second:
            names = sorted(s["name"] for s in second.get("/mcp/servers").json())
            assert names == ["generated", "sandbox"]
            assert sandbox_server(second)["enabled"] is False
