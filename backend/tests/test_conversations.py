from __future__ import annotations


def _create(client, title=None):
    response = client.post("/conversations", json={"title": title})
    assert response.status_code == 201
    return response.json()


def test_create_list_get_delete(client):
    created = _create(client, title="First")
    assert created["title"] == "First"
    assert created["message_count"] == 0

    listed = client.get("/conversations").json()
    assert [c["id"] for c in listed] == [created["id"]]

    detail = client.get(f"/conversations/{created['id']}").json()
    assert detail["id"] == created["id"]
    assert detail["messages"] == []

    deleted = client.delete(f"/conversations/{created['id']}")
    assert deleted.status_code == 204
    assert client.get(f"/conversations/{created['id']}").status_code == 404
    assert client.get("/conversations").json() == []


def test_get_missing_conversation_returns_404(client):
    assert client.get("/conversations/does-not-exist").status_code == 404


def test_delete_missing_conversation_returns_404(client):
    assert client.delete("/conversations/does-not-exist").status_code == 404


def test_send_message_persists_and_titles(client):
    created = _create(client)
    response = client.post(
        f"/conversations/{created['id']}/messages", json={"message": "hello world"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "Echo: hello world"
    assert body["stop_reason"] == "end_turn"
    assert body["iterations"] == 1
    assert body["usage"]["output_tokens"] > 0

    detail = client.get(f"/conversations/{created['id']}").json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][1]["content"] == "Echo: hello world"
    assert detail["title"] == "hello world"
    assert detail["message_count"] == 2


def test_send_message_missing_conversation_returns_404(client):
    response = client.post("/conversations/nope/messages", json={"message": "hi"})
    assert response.status_code == 404


def test_send_message_requires_non_empty_body(client):
    created = _create(client)
    response = client.post(f"/conversations/{created['id']}/messages", json={"message": ""})
    assert response.status_code == 422
