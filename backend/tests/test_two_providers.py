from __future__ import annotations

from fastapi.testclient import TestClient

from backend.services.providers.base import (
    ModelTurn,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    Usage,
)
from backend.services.providers.fake import FakeProvider
from backend.tests.helpers import enable_demo_add, register_stdio_demo


def _turn(blocks, stop):
    return ModelTurn(blocks, stop, Usage(input_tokens=5, output_tokens=3), "fake-echo")


def test_workflow_combines_two_mcp_tool_providers(make_app):
    """A local in-process server and a stdio child process in one turn."""

    def first(messages, tools):
        names = {tool.name for tool in tools}
        assert "demo__add" in names
        assert "stdio-demo__triple" in names
        return _turn(
            [
                ToolCallBlock("c1", "demo__add", {"a": 2, "b": 3}),
                ToolCallBlock("c2", "stdio-demo__triple", {"n": 4}),
            ],
            "tool_use",
        )

    def second(messages, tools):
        results = {
            block.tool_call_id: block
            for message in messages
            for block in message.blocks
            if isinstance(block, ToolResultBlock)
        }
        assert results["c1"].content.strip() == "5"
        assert results["c2"].content.strip() == "12"
        return _turn([TextBlock("5 and 12")], "end_turn")

    app = make_app(provider=FakeProvider(script=[first, second]), toolloom_allow_stdio=True)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        enable_demo_add(client)
        server = register_stdio_demo(client)
        discovered = client.post(f"/mcp/servers/{server['id']}/discover").json()
        assert discovered["added"] == 1
        client.patch(f"/mcp/servers/{server['id']}/tools/triple", json={"enabled": True})

        conversation = client.post("/conversations", json={}).json()
        body = client.post(
            f"/conversations/{conversation['id']}/messages",
            json={"message": "add 2 and 3, then triple 4"},
        ).json()

        assert body["reply"] == "5 and 12"
        assert len(body["tool_calls"]) == 2
        assert {call["name"] for call in body["tool_calls"]} == {
            "demo__add",
            "stdio-demo__triple",
        }
        assert all(call["is_error"] is False for call in body["tool_calls"])

        detail = client.get(f"/conversations/{conversation['id']}").json()
        assert [m["role"] for m in detail["messages"]] == [
            "user",
            "assistant",
            "tool",
            "tool",
            "assistant",
        ]
