from __future__ import annotations


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["provider"] == "fake"
    assert body["version"]


def test_config_status_has_no_secrets(client):
    response = client.get("/config")
    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "fake"
    assert "api_key" not in response.text.lower()


def test_models_lists_fake_provider(client):
    response = client.get("/models")
    assert response.status_code == 200
    ids = [m["id"] for m in response.json()]
    assert "fake-echo" in ids
