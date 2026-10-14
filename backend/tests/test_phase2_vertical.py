from __future__ import annotations

from fastapi.testclient import TestClient

from backend.repositories import MessageRepository
from backend.services.providers.base import (
    ModelTurn,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    Usage,
    blocks_to_dicts,
)
from backend.services.providers.fake import FakeProvider
from backend.tests.helpers import enable_demo_add, parse_sse


def _turn(blocks, stop):
    return ModelTurn(blocks, stop, Usage(input_tokens=5, output_tokens=3), "fake-echo")


def _provider(*, expect_advertised=True, expect_result_value="5"):
    def first(messages, tools):
        advertised = any(t.name == "demo__add" for t in tools)
        assert advertised is expect_advertised, f"tool advertised={advertised}"
        return _turn(
            [ToolCallBlock(id="call_1", name="demo__add", arguments={"a": 2, "b": 3})], "tool_use"
        )

    def second(messages, tools):
        results = [
            block
            for message in messages
            for block in message.blocks
            if isinstance(block, ToolResultBlock)
        ]
        assert results, "tool result must be present in the next turn's context"
        if expect_result_value is not None:
            assert results[0].content == expect_result_value
            assert results[0].is_error is False
        return _turn([TextBlock("5")], "end_turn")

    return FakeProvider(script=[first, second])


def test_demo_add_end_to_end_non_streaming(make_app):
    app = make_app(provider=_provider())
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        enable_demo_add(client)
        conversation = client.post("/conversations", json={}).json()

        response = client.post(
            f"/conversations/{conversation['id']}/messages", json={"message": "what is 2+3?"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["reply"] == "5"
        assert body["iterations"] == 2
        assert body["stop_reason"] == "end_turn"

        assert len(body["tool_calls"]) == 1
        call = body["tool_calls"][0]
        assert call["name"] == "demo__add"
        assert call["input"] == {"a": 2, "b": 3}
        assert call["output_text"] == "5"
        assert call["is_error"] is False

        detail = client.get(f"/conversations/{conversation['id']}").json()
        assert [m["role"] for m in detail["messages"]] == [
            "user",
            "assistant",
            "tool",
            "assistant",
        ]
        tool_message = detail["messages"][2]
        assert tool_message["content"] == "5"
        assert tool_message["tool_call_id"] == "call_1"
        assert tool_message["tool_name"] == "demo__add"
        assert detail["messages"][1]["blocks"][0]["type"] == "tool_use"


def test_demo_add_end_to_end_streaming(make_app):
    app = make_app(provider=_provider())
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        enable_demo_add(client)
        conversation = client.post("/conversations", json={}).json()

        with client.stream(
            "POST",
            f"/conversations/{conversation['id']}/messages/stream",
            json={"message": "2+3"},
        ) as response:
            body = "".join(response.iter_text())

        events = dict(parse_sse(body))
        assert events["tool_call_start"]["name"] == "demo__add"
        assert events["tool_call_end"]["output_text"] == "5"
        assert events["tool_call_end"]["is_error"] is False
        assert events["done"]["reply"] == "5"


def test_no_phantom_result_when_tool_disabled(make_app):
    app = make_app(provider=_provider(expect_advertised=False, expect_result_value=None))
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        # Note: we deliberately do NOT enable the tool.
        conversation = client.post("/conversations", json={}).json()
        response = client.post(
            f"/conversations/{conversation['id']}/messages", json={"message": "2+3"}
        )
        body = response.json()
        assert len(body["tool_calls"]) == 1
        assert body["tool_calls"][0]["is_error"] is True
        assert "disabled" in body["tool_calls"][0]["output_text"]

        detail = client.get(f"/conversations/{conversation['id']}").json()
        tool_message = detail["messages"][2]
        assert tool_message["blocks"][0]["is_error"] is True


def test_interrupted_turn_is_repaired_on_next_message(make_app):
    """A crash between the model's tool_use and the result must not wedge the loop."""

    def first(messages, tools):
        results = [
            block
            for message in messages
            for block in message.blocks
            if isinstance(block, ToolResultBlock)
        ]
        assert any(r.content == "[no result available]" for r in results)
        return _turn([TextBlock("continuing")], "end_turn")

    app = make_app(provider=FakeProvider(script=[first]))
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        conversation = client.post("/conversations", json={}).json()

        # Simulate a crash: assistant tool_use persisted with no tool result.
        with app.state.db.session() as session:
            repo = MessageRepository(session)
            repo.add(conversation_id=conversation["id"], role="user", content="do it")
            repo.add(
                conversation_id=conversation["id"],
                role="assistant",
                content="",
                blocks=blocks_to_dicts(
                    [ToolCallBlock(id="call_z", name="demo__add", arguments={"a": 1, "b": 2})]
                ),
            )

        response = client.post(
            f"/conversations/{conversation['id']}/messages", json={"message": "go on"}
        )
        assert response.status_code == 200
        assert response.json()["reply"] == "continuing"
