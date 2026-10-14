from __future__ import annotations

from backend.tests.helpers import parse_sse


def _create(client):
    return client.post("/conversations", json={}).json()


def test_sse_event_framing_and_persistence(client):
    created = _create(client)
    with client.stream(
        "POST",
        f"/conversations/{created['id']}/messages/stream",
        json={"message": "hi there"},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())

    events = parse_sse(body)
    kinds = [kind for kind, _ in events]
    assert "iteration_start" in kinds
    assert "token" in kinds
    assert kinds[-1] == "done"

    tokens = "".join(payload["text"] for kind, payload in events if kind == "token")
    assert tokens == "Echo: hi there"

    done = dict(events)["done"]
    assert done["reply"] == "Echo: hi there"
    assert done["stop_reason"] == "end_turn"
    assert done["usage"]["total_tokens"] > 0

    detail = client.get(f"/conversations/{created['id']}").json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]


def test_stream_missing_conversation_returns_404(client):
    response = client.post("/conversations/nope/messages/stream", json={"message": "hi"})
    assert response.status_code == 404
