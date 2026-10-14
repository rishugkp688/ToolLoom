from __future__ import annotations

from fastapi.testclient import TestClient


def _run_turn(client) -> dict:
    conversation = client.post("/conversations", json={}).json()
    client.post(f"/conversations/{conversation['id']}/messages", json={"message": "hello there"})
    return conversation


def test_usage_aggregates_without_invented_prices(make_app):
    app = make_app()
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        conversation = _run_turn(client)
        usage = client.get("/usage").json()
        assert usage["global_"]["total_tokens"] > 0
        assert conversation["id"] in usage["per_conversation"]
        # No prices configured -> tokens reported, cost is explicitly null.
        assert usage["global_"]["estimated_cost"] is None


def test_usage_cost_only_when_prices_configured(make_app):
    app = make_app(toolloom_price_input_per_1k=1.0, toolloom_price_output_per_1k=2.0)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        _run_turn(client)
        usage = client.get("/usage").json()
        assert usage["global_"]["estimated_cost"] is not None
        assert usage["global_"]["estimated_cost"] > 0


def test_config_reports_pricing_flag(make_app):
    app = make_app()
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert client.get("/config").json()["pricing_configured"] is False
        assert client.get("/config").json()["allow_stdio"] is False
