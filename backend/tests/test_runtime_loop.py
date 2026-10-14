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


def test_scripted_tool_loop_completes(make_app):
    """The loop runs a tool step, then asks the model again for a final answer."""

    def call_tool(messages, tools):
        return ModelTurn(
            [ToolCallBlock(id="call_1", name="demo__add", arguments={"a": 2, "b": 3})],
            "tool_use",
            Usage(input_tokens=3, output_tokens=2),
            "fake-echo",
        )

    def final(messages, tools):
        # A tool result for the previous call must be present in the history.
        assert any(
            isinstance(block, ToolResultBlock)
            for message in messages
            for block in message.blocks
        )
        return ModelTurn(
            [TextBlock(text="5")], "end_turn", Usage(input_tokens=4, output_tokens=1), "fake-echo"
        )

    provider = FakeProvider(script=[call_tool, final])
    app = make_app(provider=provider)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        created = client.post("/conversations", json={}).json()
        response = client.post(
            f"/conversations/{created['id']}/messages", json={"message": "2+3"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["reply"] == "5"
        assert body["iterations"] == 2
        assert len(body["tool_calls"]) == 1
        assert body["tool_calls"][0]["name"] == "demo__add"
        # No executor is wired in Phase 0, so the call is an explicit error.
        assert body["tool_calls"][0]["is_error"] is True

        detail = client.get(f"/conversations/{created['id']}").json()
        assert [m["role"] for m in detail["messages"]] == [
            "user",
            "assistant",
            "tool",
            "assistant",
        ]
