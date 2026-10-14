from __future__ import annotations

import asyncio

import pytest

from backend.services.providers.base import ToolCallBlock
from backend.services.sandbox.http_tool import HttpFetchError, fetch
from backend.tests.helpers import enable_tool


class _Ctx:
    secrets = None


def _execute(client, name: str, arguments: dict):
    return asyncio.run(
        client.app.state.tool_executor.execute(ToolCallBlock("c1", name, arguments), "conv")
    )


def test_http_fetch_is_high_risk(client):
    servers = client.get("/mcp/servers").json()
    sandbox = next(s for s in servers if s["name"] == "sandbox")
    tool = next(
        t
        for t in client.get(f"/mcp/servers/{sandbox['id']}/tools").json()
        if t["name"] == "http_fetch"
    )
    assert tool["risk"] == "high"
    assert tool["approved"] is False
    assert tool["enabled"] is False


def test_high_risk_tool_needs_enable_and_approval(client):
    enable_tool(client, "sandbox", "http_fetch")  # enabled but NOT approved
    result = _execute(client, "sandbox__http_fetch", {"url": "http://example.com"})
    assert result.is_error is True
    assert "requires explicit operator approval" in result.model_text


def test_approval_then_loopback_is_refused(client):
    enable_tool(client, "sandbox", "http_fetch")
    servers = client.get("/mcp/servers").json()
    sandbox = next(s for s in servers if s["name"] == "sandbox")
    client.patch(f"/mcp/servers/{sandbox['id']}/tools/http_fetch", json={"approved": True})

    result = _execute(client, "sandbox__http_fetch", {"url": "http://127.0.0.1:8000/"})
    assert result.is_error is True
    assert "non-public address" in result.model_text


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file",
        "file:///etc/passwd",
        "http://127.0.0.1/",
        "http://localhost/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
    ],
)
def test_http_fetch_blocks_unsafe_targets(url):
    with pytest.raises(HttpFetchError):
        fetch({"url": url}, _Ctx())


def test_http_fetch_rejects_literal_auth_header():
    with pytest.raises(HttpFetchError):
        fetch({"url": "http://example.com", "headers": {"Authorization": "Bearer literal"}}, _Ctx())


def test_http_fetch_rejects_bad_method():
    with pytest.raises(HttpFetchError):
        fetch({"url": "http://example.com", "method": "TRACE"}, _Ctx())


def test_http_fetch_requires_url():
    with pytest.raises(HttpFetchError):
        fetch({}, _Ctx())
