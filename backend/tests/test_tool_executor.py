from __future__ import annotations

from backend.services.providers.base import ToolCallBlock
from backend.tests.helpers import enable_demo_add


async def _exec(client, name: str, arguments: dict):
    call = ToolCallBlock(id="call_1", name=name, arguments=arguments)
    return await client.app.state.tool_executor.execute(call, "conv-test")


async def test_valid_call_returns_sum(client):
    enable_demo_add(client)
    result = await _exec(client, "demo__add", {"a": 2, "b": 3})
    assert result.is_error is False
    assert result.output_text == "5"
    assert result.model_text == "5"
    assert result.tool_call_id == "call_1"


async def test_disabled_tool_is_rejected(client):
    # demo__add is disabled by default.
    result = await _exec(client, "demo__add", {"a": 1, "b": 1})
    assert result.is_error is True
    assert "disabled" in result.model_text


async def test_unknown_tool_is_rejected(client):
    enable_demo_add(client)
    result = await _exec(client, "demo__nope", {})
    assert result.is_error is True
    assert "unknown tool" in result.model_text


async def test_unknown_server_is_rejected(client):
    result = await _exec(client, "ghost__add", {})
    assert result.is_error is True
    assert "unknown MCP server" in result.model_text


async def test_invalid_namespaced_name_is_rejected(client):
    result = await _exec(client, "no_separator", {})
    assert result.is_error is True
    assert "invalid namespaced" in result.model_text


async def test_schema_validation_rejects_wrong_types(client):
    enable_demo_add(client)
    result = await _exec(client, "demo__add", {"a": "two", "b": 3})
    assert result.is_error is True
    assert "invalid tool arguments" in result.model_text
    assert result.validation_errors


async def test_schema_validation_rejects_missing_required(client):
    enable_demo_add(client)
    result = await _exec(client, "demo__add", {"a": 1})
    assert result.is_error is True
    assert "invalid tool arguments" in result.model_text


async def test_schema_validation_rejects_extra_properties(client):
    enable_demo_add(client)
    result = await _exec(client, "demo__add", {"a": 1, "b": 2, "c": 3})
    assert result.is_error is True
    assert "additional" in result.model_text.lower()
