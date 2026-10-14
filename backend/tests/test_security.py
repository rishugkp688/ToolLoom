from __future__ import annotations


def test_cross_origin_post_rejected(client):
    response = client.post(
        "/conversations", json={}, headers={"Origin": "http://evil.example"}
    )
    assert response.status_code == 403


def test_local_origin_post_allowed(client):
    response = client.post(
        "/conversations", json={}, headers={"Origin": "http://127.0.0.1:8000"}
    )
    assert response.status_code == 201


def test_foreign_host_post_rejected(client):
    response = client.post(
        "/conversations", json={}, headers={"Host": "evil.example"}
    )
    assert response.status_code == 403


def test_get_is_not_blocked_by_origin(client):
    # Safe methods are not subject to the origin check.
    assert client.get("/health", headers={"Origin": "http://evil.example"}).status_code == 200
