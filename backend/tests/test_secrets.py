from __future__ import annotations

import json

import pytest

from backend.services.secrets import DictResolver, SecretsError, preview


def test_upsert_and_list_never_return_plaintext(client):
    response = client.post(
        "/secrets",
        json={"name": "my_token", "value": "super-secret-value-123", "description": "demo"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "my_token"
    assert "value" not in body
    assert "super-secret-value-123" not in json.dumps(body)

    listing = client.get("/secrets").json()
    assert listing[0]["preview"]
    assert "super-secret-value-123" not in json.dumps(listing)


def test_delete_secret(client):
    client.post("/secrets", json={"name": "temp", "value": "abc"})
    assert client.delete("/secrets/temp").status_code == 204
    assert client.delete("/secrets/temp").status_code == 404


def test_invalid_secret_name_is_rejected(client):
    response = client.post("/secrets", json={"name": "BAD NAME", "value": "x"})
    assert response.status_code == 400


def test_resolver_substitutes_references():
    resolver = DictResolver({"token": "abc"})
    assert resolver.resolve_string("Bearer ${secrets.token}") == "Bearer abc"
    assert resolver.resolve_mapping({"Authorization": "${secrets.TOKEN}"}) == {
        "Authorization": "abc"
    }


def test_missing_reference_raises():
    with pytest.raises(SecretsError):
        DictResolver({}).resolve_string("Bearer ${secrets.nope}")


def test_db_resolver_roundtrip_through_api(client):
    client.post("/secrets", json={"name": "my_token", "value": "abc123"})
    resolver = client.app.state.secret_resolver
    assert resolver.resolve_string("x${secrets.my_token}y") == "xabc123y"


def test_preview_masks_short_values():
    assert preview("short") == "•••"
    assert preview("longenoughvalue").startswith("long")
