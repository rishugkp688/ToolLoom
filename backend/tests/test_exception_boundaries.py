from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from backend.services.providers.base import ModelTurn, ProviderError, TextBlock, TextDelta, Usage
from backend.services.providers.fake import FakeProvider
from backend.tests.conftest import LOCAL_BASE_URL
from backend.tests.helpers import parse_sse


class BrokenStream(FakeProvider):
    async def stream(self, **kwargs):
        yield TextDelta("Partial answer before disconnect.")
        raise ProviderError("Connection failed")


def test_partial_answer_survives_provider_failure_and_reload(make_app):
    with TestClient(make_app(BrokenStream()), base_url=LOCAL_BASE_URL) as client:
        cid = client.post("/conversations", json={}).json()["id"]
        events = parse_sse(
            client.post(f"/conversations/{cid}/messages/stream", json={"message": "Hi"}).text
        )
        assert any(kind == "error" for kind, _ in events)
        assert not any(kind == "done" for kind, _ in events)
        rows = client.get(f"/conversations/{cid}").json()["messages"]
        assert rows[-2]["content"] == "Partial answer before disconnect."
        assert rows[-2]["stop_reason"] == "error"
        assert rows[-1]["stop_reason"] == "error"
        assert not client.app.state.active_runs


def test_partial_answer_survives_timeout(make_app):
    class Slow(FakeProvider):
        async def stream(self, **kwargs):
            yield TextDelta("Started answering.")
            await asyncio.sleep(5)

    with TestClient(
        make_app(Slow(), toolloom_run_timeout_seconds=1), base_url=LOCAL_BASE_URL
    ) as client:
        cid = client.post("/conversations", json={}).json()["id"]
        response = client.post(f"/conversations/{cid}/messages", json={"message": "Hi"})
        assert response.status_code == 504
        assert "time limit" in response.json()["detail"]
        rows = client.get(f"/conversations/{cid}").json()["messages"]
        assert rows[-2]["content"] == "Started answering."
        assert rows[-1]["stop_reason"] == "cancelled"


def test_title_metadata_failure_cannot_fail_completed_reply(make_app, monkeypatch):
    app = make_app(toolloom_auto_titles=True)

    async def fail(*args):
        raise RuntimeError("Title metadata unavailable")

    monkeypatch.setattr(app.state.conversation_titles, "generate", fail)
    with TestClient(app, base_url=LOCAL_BASE_URL) as client:
        cid = client.post("/conversations", json={}).json()["id"]
        events = parse_sse(
            client.post(f"/conversations/{cid}/messages/stream", json={"message": "Hi"}).text
        )
        assert events[-1][0] == "done"
        assert client.get(f"/conversations/{cid}").json()["messages"][-1]["content"] == "Echo: Hi"


def test_database_failure_while_recording_error_does_not_mask_provider_error(make_app, monkeypatch):
    app = make_app(BrokenStream())

    def fail(*args):
        raise RuntimeError("Database unavailable")

    monkeypatch.setattr(app.state.runtime, "_persist_note", fail)
    with TestClient(app, base_url=LOCAL_BASE_URL) as client:
        cid = client.post("/conversations", json={}).json()["id"]
        result = client.post(f"/conversations/{cid}/messages", json={"message": "Hi"})
        assert result.status_code == 502
        assert "Connection failed" in result.json()["detail"]
        assert not app.state.active_runs


def test_incomplete_provider_turn_is_never_a_done_event(make_app):
    app = make_app(
        FakeProvider(script=[ModelTurn([TextBlock("Incomplete")], "error", Usage(), "fake")])
    )
    with TestClient(app, base_url=LOCAL_BASE_URL) as client:
        cid = client.post("/conversations", json={}).json()["id"]
        events = parse_sse(
            client.post(f"/conversations/{cid}/messages/stream", json={"message": "Hi"}).text
        )
        assert events[-1][0] == "error"
        assert not any(kind == "done" for kind, _ in events)


def test_uncaught_api_failure_is_json_and_does_not_leak_secrets(client, monkeypatch, caplog):
    def fail(*args):
        raise RuntimeError("sensitive-credential")

    monkeypatch.setattr("backend.api.settings.SettingRepository.get", fail)
    result = client.get("/settings")
    assert result.status_code == 500
    assert "Internal server error" in result.json()["detail"]
    assert "sensitive-credential" not in result.text + caplog.text


@pytest.mark.parametrize(
    "origin",
    [
        "http://127.0.0.1:9999",
        "https://127.0.0.1:8000",
        "http://localhost:8000",
        "http://user:pass@127.0.0.1:8000",
        "http://127.0.0.1:8000/path",
    ],
)
def test_other_local_origins_are_rejected(client, origin):
    assert client.post("/conversations", json={}, headers={"Origin": origin}).status_code == 403
