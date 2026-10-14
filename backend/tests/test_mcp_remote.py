from __future__ import annotations

import socket
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient
from mcp.server.fastmcp import FastMCP

from backend.services.providers.base import (
    ModelTurn,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    Usage,
)
from backend.services.providers.fake import FakeProvider


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_port(port: int, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"test MCP server did not start on port {port}")


class _RunningServer:
    def __init__(self, app, port: int) -> None:
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> None:
        self.thread.start()
        _wait_for_port(self.server.config.port)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


def _build_fastmcp() -> FastMCP:
    server = FastMCP("toolloom-remote")

    @server.tool()
    def multiply(a: int, b: int) -> int:
        """Multiply two integers and return the product."""
        return a * b

    return server


@pytest.fixture
def remote_streamable_http():
    running = _RunningServer(_build_fastmcp().streamable_http_app(), _free_port())
    running.start()
    try:
        yield {"name": "remote", "transport": "http", "url": f"http://127.0.0.1:{running.server.config.port}/mcp"}
    finally:
        running.stop()


@pytest.fixture
def remote_sse():
    running = _RunningServer(_build_fastmcp().sse_app(), _free_port())
    running.start()
    try:
        yield {"name": "remote-sse", "transport": "sse", "url": f"http://127.0.0.1:{running.server.config.port}/sse"}
    finally:
        running.stop()


def _turn(blocks, stop):
    return ModelTurn(blocks, stop, Usage(input_tokens=5, output_tokens=3), "fake-echo")


def _remote_provider(tool_name: str, expected: str):
    def first(messages, tools):
        assert any(t.name == tool_name for t in tools), f"{tool_name} must be advertised"
        return _turn(
            [ToolCallBlock(id="call_r", name=tool_name, arguments={"a": 6, "b": 7})], "tool_use"
        )

    def second(messages, tools):
        results = [
            block
            for message in messages
            for block in message.blocks
            if isinstance(block, ToolResultBlock)
        ]
        assert results and results[0].content.strip() == expected
        assert results[0].is_error is False
        return _turn([TextBlock(expected)], "end_turn")

    return FakeProvider(script=[first, second])


def test_remote_mcp_discover_enable_invoke_persist(make_app, remote_streamable_http):
    app = make_app(provider=_remote_provider("remote__multiply", "42"))
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        created = client.post("/mcp/servers", json={**remote_streamable_http, "enabled": True})
        assert created.status_code == 201
        server = created.json()

        assert client.post(f"/mcp/servers/{server['id']}/test").json()["ok"] is True

        discovered = client.post(f"/mcp/servers/{server['id']}/discover").json()
        assert discovered["added"] == 1
        tools = client.get(f"/mcp/servers/{server['id']}/tools").json()
        tool = next(t for t in tools if t["namespaced_name"] == "remote__multiply")
        assert tool["enabled"] is False  # default-deny even for remote discovery

        enabled = client.patch(
            f"/mcp/servers/{server['id']}/tools/multiply", json={"enabled": True}
        )
        assert enabled.status_code == 200 and enabled.json()["enabled"] is True

        conversation = client.post("/conversations", json={}).json()
        body = client.post(
            f"/conversations/{conversation['id']}/messages", json={"message": "what is 6*7?"}
        ).json()
        assert body["reply"] == "42"
        assert len(body["tool_calls"]) == 1
        assert body["tool_calls"][0]["name"] == "remote__multiply"
        assert body["tool_calls"][0]["output_text"].strip() == "42"
        assert body["tool_calls"][0]["is_error"] is False

        detail = client.get(f"/conversations/{conversation['id']}").json()
        assert [m["role"] for m in detail["messages"]] == [
            "user",
            "assistant",
            "tool",
            "assistant",
        ]
        assert detail["messages"][2]["content"].strip() == "42"


def test_remote_sse_transport(make_app, remote_sse):
    app = make_app(provider=_remote_provider("remote-sse__multiply", "42"))
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        created = client.post("/mcp/servers", json={**remote_sse, "enabled": True}).json()
        discovered = client.post(f"/mcp/servers/{created['id']}/discover").json()
        assert discovered["added"] == 1
        client.patch(f"/mcp/servers/{created['id']}/tools/multiply", json={"enabled": True})

        conversation = client.post("/conversations", json={}).json()
        body = client.post(
            f"/conversations/{conversation['id']}/messages", json={"message": "6*7"}
        ).json()
        assert body["reply"] == "42"
        assert body["tool_calls"][0]["output_text"].strip() == "42"


def test_unreachable_remote_server_returns_502(client):
    created = client.post(
        "/mcp/servers",
        json={"name": "dead", "transport": "http", "url": "http://127.0.0.1:1/mcp", "enabled": True},
    ).json()
    response = client.post(f"/mcp/servers/{created['id']}/discover")
    assert response.status_code == 502
    assert "dead" in response.text or "Connect" in response.text or "URL" in response.text
