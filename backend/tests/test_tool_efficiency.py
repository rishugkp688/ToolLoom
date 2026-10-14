from __future__ import annotations

import asyncio
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.db import Database
from backend.services.context_builder import ContextBuilder
from backend.services.providers.base import ModelTurn, TextBlock, ToolCallBlock, Usage
from backend.services.providers.fake import FakeProvider
from backend.services.sandbox import http_tool
from backend.tests.conftest import LOCAL_BASE_URL
from backend.tests.helpers import enable_demo_add, enable_tool, parse_sse
from backend.tests.test_skills import SKILL_MD, _upload


def tool_turn(index, args=None):
    return ModelTurn(
        [
            TextBlock("Checking."),
            ToolCallBlock(f"call-{index}", "demo__add", args or {"a": index, "b": 1}),
        ],
        "tool_use",
        Usage(1, 1),
        "fake-echo",
    )


def finish(messages, tools):
    assert tools == []
    return ModelTurn(
        [TextBlock("Here is what I found so far.")], "end_turn", Usage(1, 1), "fake-echo"
    )


def test_skills_hidden_when_empty_disabled_or_deleted_and_rechecked_at_execution(client):
    for name in ["skill_list", "skill_read"]:
        enable_tool(client, "sandbox", name)
    cid = client.post("/conversations", json={}).json()["id"]

    def offered():
        return {
            t.name
            for t in ContextBuilder(client.app.state.db, client.app.state.settings).build(cid).tools
        }

    assert not {"sandbox__skill_list", "sandbox__skill_read"} & offered()
    _upload(client, "time.md", SKILL_MD)
    skill = client.get("/skills").json()[0]
    assert "sandbox__skill_list" not in offered()
    client.patch(f"/skills/{skill['id']}", json={"enabled": True})
    assert {"sandbox__skill_list", "sandbox__skill_read"} <= offered()
    client.patch(f"/skills/{skill['id']}", json={"enabled": False})
    result = asyncio.run(
        client.app.state.tool_executor.execute(
            ToolCallBlock("stale", "sandbox__skill_list", {}),
            cid,
        )
    )
    assert result.is_error and "unavailable" in result.model_text
    client.delete(f"/skills/{skill['id']}")
    assert "sandbox__skill_read" not in offered()


@pytest.mark.parametrize("unlimited", [False, True])
def test_run_mode_caps_and_final_answer(make_app, unlimited):
    provider = FakeProvider(
        script=[
            tool_turn(1),
            tool_turn(2),
            finish
            if not unlimited
            else ModelTurn([TextBlock("The final answer.")], "end_turn", Usage(1, 1), "fake-echo"),
        ]
    )
    app = make_app(provider, toolloom_max_iterations=2, toolloom_max_tool_calls=2)
    with TestClient(app, base_url=LOCAL_BASE_URL) as client:
        enable_demo_add(client)
        client.put("/settings", json={"unlimited_tools": unlimited})
        cid = client.post("/conversations", json={}).json()["id"]
        result = client.post(f"/conversations/{cid}/messages", json={"message": "Calculate"}).json()
        assert result["stop_reason"] == ("end_turn" if unlimited else "max_iterations")
        assert result["reply"] == (
            "The final answer." if unlimited else "Here is what I found so far."
        )
        assert result["iterations"] == 3
        assert len(result["tool_calls"]) == 2
        assert not any(t["is_error"] for t in result["tool_calls"])


def test_unlimited_allows_many_distinct_steps_beyond_configured_caps(make_app):
    provider = FakeProvider(
        script=[tool_turn(i) for i in range(1, 16)]
        + [
            ModelTurn([TextBlock("Finished all fifteen.")], "end_turn", Usage(), "fake-echo"),
        ]
    )
    with TestClient(
        make_app(provider, toolloom_max_iterations=1, toolloom_max_tool_calls=1),
        base_url=LOCAL_BASE_URL,
    ) as client:
        enable_demo_add(client)
        client.put("/settings", json={"unlimited_tools": True})
        cid = client.post("/conversations", json={}).json()["id"]
        result = client.post(f"/conversations/{cid}/messages", json={"message": "Run task"}).json()
        assert result["stop_reason"] == "end_turn" and result["iterations"] == 16
        assert len(result["tool_calls"]) == 15
        assert all(not call["is_error"] for call in result["tool_calls"])


def test_unlimited_still_blocks_duplicate_work_and_requests_final_summary(make_app):
    provider = FakeProvider(script=[tool_turn(i, {"a": 2, "b": 3}) for i in range(1, 4)] + [finish])
    with TestClient(make_app(provider), base_url=LOCAL_BASE_URL) as client:
        enable_demo_add(client)
        client.put("/settings", json={"unlimited_tools": True})
        cid = client.post("/conversations", json={}).json()["id"]
        result = client.post(f"/conversations/{cid}/messages", json={"message": "Task"}).json()
        assert result["stop_reason"] == "loop_guard"
        assert [c["is_error"] for c in result["tool_calls"]] == [False, False, True]
        assert "refused" in result["tool_calls"][-1]["output_text"]
        assert result["reply"] == "Here is what I found so far."
        assert (
            client.get(f"/conversations/{cid}").json()["messages"][-1]["stop_reason"]
            == "loop_guard"
        )


def test_batch_cap_persists_refusals_for_every_unexecuted_call(make_app):
    calls = [ToolCallBlock(f"b{i}", "demo__add", {"a": i, "b": 1}) for i in range(4)]
    provider = FakeProvider(script=[ModelTurn(calls, "tool_use", Usage(), "fake-echo"), finish])
    with TestClient(
        make_app(provider, toolloom_max_tool_calls=1), base_url=LOCAL_BASE_URL
    ) as client:
        enable_demo_add(client)
        cid = client.post("/conversations", json={}).json()["id"]
        result = client.post(f"/conversations/{cid}/messages", json={"message": "Task"}).json()
        assert [c["is_error"] for c in result["tool_calls"]] == [False, True, True, True]
        rows = client.get(f"/conversations/{cid}").json()["messages"]
        assert {m["tool_call_id"] for m in rows if m["role"] == "tool"} == {"b0", "b1", "b2", "b3"}


def test_unlimited_does_not_enable_tools_or_grant_approval(make_app):
    provider = FakeProvider(
        script=[
            ModelTurn(
                [
                    ToolCallBlock("disabled", "sandbox__fs_write", {"path": "x", "content": "x"}),
                    ToolCallBlock(
                        "unapproved", "sandbox__http_fetch", {"url": "https://example.com"}
                    ),
                ],
                "tool_use",
                Usage(),
                "fake-echo",
            )
        ]
    )
    with TestClient(make_app(provider), base_url=LOCAL_BASE_URL) as client:
        enable_tool(client, "sandbox", "http_fetch")
        client.put("/settings", json={"unlimited_tools": True})
        cid = client.post("/conversations", json={}).json()["id"]
        result = client.post(f"/conversations/{cid}/messages", json={"message": "Task"}).json()
        assert all(c["is_error"] for c in result["tool_calls"])
        assert "disabled" in result["tool_calls"][0]["output_text"]
        assert "approval" in result["tool_calls"][1]["output_text"]


def test_unlimited_preserves_run_timeout(make_app):
    class Slow(FakeProvider):
        async def stream(self, **kwargs):
            await asyncio.sleep(5)
            async for event in super().stream(**kwargs):
                yield event

    app = make_app(Slow(), toolloom_run_timeout_seconds=1)
    with TestClient(app, base_url=LOCAL_BASE_URL) as client:
        client.put("/settings", json={"unlimited_tools": True})
        cid = client.post("/conversations", json={}).json()["id"]
        events = parse_sse(
            client.post(f"/conversations/{cid}/messages/stream", json={"message": "Hi"}).text
        )
        assert any(kind == "error" and "time limit" in data["message"] for kind, data in events)
        assert cid not in app.state.active_runs


def test_run_setting_partial_update_and_migration(client, tmp_path):
    assert client.get("/settings").json()["unlimited_tools"] is False
    client.put("/settings", json={"system_prompt": "Keep this prompt"})
    assert (
        client.put("/settings", json={"unlimited_tools": True}).json()["system_prompt"]
        == "Keep this prompt"
    )
    assert (
        client.put("/settings", json={"system_prompt": "Updated"}).json()["unlimited_tools"] is True
    )
    path = tmp_path / "old-settings.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE settings (id TEXT PRIMARY KEY, system_prompt TEXT)")
        conn.execute("INSERT INTO settings VALUES ('global', 'Original')")
    db = Database(f"sqlite:///{path}")
    try:
        db._apply_lightweight_migrations()
        db._apply_lightweight_migrations()
        with sqlite3.connect(path) as conn:
            assert conn.execute(
                "SELECT system_prompt, unlimited_tools FROM settings"
            ).fetchone() == ("Original", 0)
    finally:
        db.dispose()


def test_html_fetch_returns_readable_text_and_links_without_scripts():
    raw = b'<html><style>noise</style><h1>Inference</h1><script>noise()</script><p>Fast &amp; cheap.</p><a href="https://example.com/paper">Paper</a></html>'
    rendered = http_tool._render(httpx.Response(200, headers={"Content-Type": "text/html"}), raw)
    assert "Inference" in rendered and "Fast & cheap." in rendered
    assert "Paper (https://example.com/paper)" in rendered
    assert "noise" not in rendered and "<html>" not in rendered


def test_http_error_status_is_an_error_result(monkeypatch):
    monkeypatch.setattr(http_tool, "_resolve_and_check", lambda host: None)
    factory = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(403, text="Access denied"))
    monkeypatch.setattr(
        http_tool.httpx, "Client", lambda **kwargs: factory(transport=transport, **kwargs)
    )
    with pytest.raises(http_tool.HttpFetchError, match="HTTP 403"):
        http_tool.fetch({"url": "https://example.com"}, object())


def test_provider_cannot_execute_tools_during_finalization(make_app):
    def ignore_tools(messages, tools):
        assert tools == []
        return tool_turn(2)

    provider = FakeProvider(script=[tool_turn(1), ignore_tools])
    with TestClient(
        make_app(provider, toolloom_max_iterations=1), base_url=LOCAL_BASE_URL
    ) as client:
        enable_demo_add(client)
        cid = client.post("/conversations", json={}).json()["id"]
        result = client.post(f"/conversations/{cid}/messages", json={"message": "Task"}).json()
        assert result["stop_reason"] == "max_iterations"
        assert [c["is_error"] for c in result["tool_calls"]] == [False, True]
        assert "not executed" in result["tool_calls"][-1]["output_text"]
        rows = client.get(f"/conversations/{cid}").json()["messages"]
        assert rows[-1]["stop_reason"] == "max_iterations"
