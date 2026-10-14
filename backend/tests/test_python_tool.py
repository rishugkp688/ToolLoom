from __future__ import annotations

import asyncio
import json
import os

import pytest
from fastapi.testclient import TestClient

from backend.services.providers.base import ModelTurn, TextBlock, ToolCallBlock, Usage
from backend.services.providers.fake import FakeProvider


def sandbox(client):
    return next(s for s in client.get("/mcp/servers").json() if s["name"] == "sandbox")


def enable_python(client):
    server = sandbox(client)
    endpoint = f"/mcp/servers/{server['id']}/tools/python"
    assert client.patch(endpoint, json={"enabled": True, "approved": True}).status_code == 200
    return endpoint


def test_python_is_disabled_and_risk_cannot_be_downgraded(client):
    endpoint = f"/mcp/servers/{sandbox(client)['id']}/tools/python"
    assert client.patch(endpoint, json={"enabled": True}).status_code == 400
    assert client.patch(endpoint, json={"risk": "low"}).status_code == 400


@pytest.mark.parametrize(
    ("code", "reason", "error"),
    [
        ("print(2 + 3)", "exited", False),
        ('raise ValueError("deliberate")', "exited", True),
        ("import time; time.sleep(10)", "timeout", True),
        ('while True: print("x" * 8192)', "output_limit", True),
        ("import sys; sys.exit(7)", "exited", True),
    ],
)
async def test_python_execution_limits_and_errors(make_app, code, reason, error):
    with TestClient(
        make_app(
            toolloom_allow_python=True,
            toolloom_python_timeout_seconds=1,
            toolloom_python_output_bytes=4096,
        ),
        base_url="http://127.0.0.1",
    ) as client:
        enable_python(client)
        result = await asyncio.wait_for(
            client.app.state.tool_executor.execute(
                ToolCallBlock("python-case", "sandbox__python", {"code": code}), "python-case"
            ),
            10,
        )
        assert result.is_error is error
        output = json.loads(result.output_text)
        assert output["stop_reason"] == reason
        assert len(output["stdout"].encode()) + len(output["stderr"].encode()) <= 4096
        if code == "print(2 + 3)":
            assert output["stdout"].strip() == "5"


async def test_python_clean_environment_and_workspace(make_app, monkeypatch):
    monkeypatch.setenv("PRIVATE_TEST_TOKEN", "must-not-inherit")
    with TestClient(make_app(toolloom_allow_python=True), base_url="http://127.0.0.1") as client:
        enable_python(client)
        code = 'import os; from pathlib import Path; print(os.getenv("PRIVATE_TEST_TOKEN", "absent")); Path("result.txt").write_text("42")'
        result = await client.app.state.tool_executor.execute(
            ToolCallBlock("p", "sandbox__python", {"code": code}), "python-files"
        )
        assert not result.is_error
        assert json.loads(result.output_text)["stdout"].strip() == "absent"
        assert (
            client.app.state.settings.workspaces_dir / "python-files/result.txt"
        ).read_text() == "42"


async def test_python_cancellation_stops_delayed_writes(make_app):
    with TestClient(make_app(toolloom_allow_python=True), base_url="http://127.0.0.1") as client:
        enable_python(client)
        code = 'from pathlib import Path; import time; Path("started").write_text("yes"); time.sleep(1); Path("late").write_text("bad")'
        task = asyncio.create_task(
            client.app.state.tool_executor.execute(
                ToolCallBlock("p", "sandbox__python", {"code": code}), "python-cancel"
            )
        )
        root = client.app.state.settings.workspaces_dir / "python-cancel"
        for _ in range(100):
            if (root / "started").exists():
                break
            await asyncio.sleep(0.02)
        assert (root / "started").exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(1.1)
        assert not (root / "late").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group cleanup")
async def test_python_cancellation_stops_child_processes(make_app):
    with TestClient(make_app(toolloom_allow_python=True), base_url="http://127.0.0.1") as client:
        enable_python(client)
        child = (
            'from pathlib import Path; import time; Path("child-started").touch(); '
            'time.sleep(1); Path("child-late").touch()'
        )
        code = f'import subprocess, sys, time; subprocess.Popen([sys.executable, "-c", {child!r}]); time.sleep(10)'
        task = asyncio.create_task(
            client.app.state.tool_executor.execute(
                ToolCallBlock("p", "sandbox__python", {"code": code}), "python-child"
            )
        )
        root = client.app.state.settings.workspaces_dir / "python-child"
        for _ in range(100):
            if (root / "child-started").exists():
                break
            await asyncio.sleep(0.02)
        assert (root / "child-started").exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(1.1)
        assert not (root / "child-late").exists()


def test_python_full_runtime_roundtrip(make_app):
    provider = FakeProvider(
        script=[
            ModelTurn(
                [ToolCallBlock("p", "sandbox__python", {"code": "print(sum(range(11)))"})],
                "tool_use",
                Usage(5, 3),
                "fake-echo",
            ),
            ModelTurn([TextBlock("55")], "end_turn", Usage(5, 3), "fake-echo"),
        ]
    )
    with TestClient(
        make_app(provider=provider, toolloom_allow_python=True), base_url="http://127.0.0.1"
    ) as client:
        enable_python(client)
        cid = client.post("/conversations", json={}).json()["id"]
        result = client.post(
            f"/conversations/{cid}/messages", json={"message": "sum 0..10 with Python"}
        ).json()
        assert result["reply"] == "55" and result["tool_calls"][0]["is_error"] is False
        detail = client.get(f"/conversations/{cid}").json()
        assert [m["role"] for m in detail["messages"]] == ["user", "assistant", "tool", "assistant"]
        assert json.loads(detail["messages"][2]["content"])["stdout"].strip() == "55"
        assert detail["messages"][1]["stop_reason"] == "tool_use"
        assert detail["messages"][3]["stop_reason"] == "end_turn"
