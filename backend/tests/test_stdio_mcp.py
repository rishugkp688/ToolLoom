from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from backend.services.providers.base import ToolCallBlock
from backend.tests.helpers import register_stdio_demo


def _execute(client, name: str, arguments: dict, conversation_id: str = "conv"):
    return asyncio.run(
        client.app.state.tool_executor.execute(
            ToolCallBlock("call_1", name, arguments), conversation_id
        )
    )


def test_stdio_rejected_when_disabled(client):
    response = client.post(
        "/mcp/servers", json={"name": "nope", "transport": "stdio", "command": "echo"}
    )
    assert response.status_code == 400
    assert "TOOLLOOM_ALLOW_STDIO" in response.json()["detail"]


def test_stdio_lifecycle_discover_enable_invoke(make_app):
    app = make_app(toolloom_allow_stdio=True)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        server = register_stdio_demo(client)

        assert client.post(f"/mcp/servers/{server['id']}/test").json()["ok"] is True

        discovered = client.post(f"/mcp/servers/{server['id']}/discover").json()
        assert discovered["added"] == 1
        tool = discovered["tools"][0]
        assert tool["namespaced_name"] == "stdio-demo__triple"
        assert tool["enabled"] is False  # default-deny, even for stdio

        client.patch(f"/mcp/servers/{server['id']}/tools/triple", json={"enabled": True})

        result = _execute(client, "stdio-demo__triple", {"n": 7})
        assert result.is_error is False
        assert result.output_text.strip() == "21"


def test_stdio_command_is_fixed_not_model_supplied(make_app):
    """There is no tool the model can call to create or run a stdio command."""
    app = make_app(toolloom_allow_stdio=True)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        tools = []
        for server in client.get("/mcp/servers").json():
            tools.extend(
                t["namespaced_name"] for t in client.get(f"/mcp/servers/{server['id']}/tools").json()
            )
        assert not any("stdio" in name or "shell" in name for name in tools)
