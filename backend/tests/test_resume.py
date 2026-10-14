from __future__ import annotations

from fastapi.testclient import TestClient

from backend.services.providers.base import ModelTurn, TextBlock, Usage
from backend.services.providers.fake import FakeProvider
from backend.tests.helpers import parse_sse


def _turn(blocks, stop="end_turn"):
    return ModelTurn(blocks, stop, Usage(input_tokens=3, output_tokens=2), "fake-echo")


def test_continue_injects_resume_context(make_app):
    captured: dict = {}

    def first(messages, tools):
        text = "\n".join(
            block.text for message in messages for block in message.blocks if hasattr(block, "text")
        )
        captured["resume"] = "<resume_context>" in text
        return _turn([TextBlock("done")])

    app = make_app(provider=FakeProvider(script=[first]))
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        conversation = client.post("/conversations", json={}).json()
        response = client.post(f"/conversations/{conversation['id']}/continue")
        assert response.status_code == 200
        assert response.json()["reply"] == "done"
        assert captured["resume"] is True


def test_continue_missing_conversation_returns_404(client):
    assert client.post("/conversations/nope/continue").status_code == 404


def test_normal_turn_has_no_resume_context(make_app):
    captured: dict = {}

    def first(messages, tools):
        text = "\n".join(
            block.text for message in messages for block in message.blocks if hasattr(block, "text")
        )
        captured["resume"] = "<resume_context>" in text
        return _turn([TextBlock("ok")])

    app = make_app(provider=FakeProvider(script=[first]))
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        conversation = client.post("/conversations", json={}).json()
        client.post(f"/conversations/{conversation['id']}/messages", json={"message": "hello"})
        assert captured["resume"] is False


def test_stream_emits_compaction_events(client):
    conversation = client.post("/conversations", json={}).json()
    with client.stream(
        "POST", f"/conversations/{conversation['id']}/messages/stream", json={"message": "hi"}
    ) as response:
        kinds = [kind for kind, _ in parse_sse("".join(response.iter_text()))]
    assert "compacting" in kinds
