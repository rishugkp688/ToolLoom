from __future__ import annotations

import json

import httpx
import pytest

from backend.config import Settings
from backend.services.providers.base import (
    MessageStopEvent,
    ProviderError,
    ProviderMessage,
    TextBlock,
    TextDelta,
    ToolCallBlock,
    ToolCallDeltaEvent,
    ToolCallStartEvent,
    ToolSpec,
)
from backend.services.providers.commandcode import CommandCodeProvider

BASE = "http://provider.test/provider/v1"


class _SSEStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        pass


def make_settings(**overrides) -> Settings:
    values = dict(
        _env_file=None,
        llm_provider="commandcode",
        commandcode_api_key="test-key-123",
        commandcode_base_url=BASE,
    )
    values.update(overrides)
    return Settings(**values)


def data_frame(payload) -> bytes:
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return f"data: {body}\n\n".encode()


def event_frame(event: str, payload) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode()


def make_provider(handler, **overrides) -> CommandCodeProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return CommandCodeProvider(make_settings(**overrides), client=client)


async def collect(provider: CommandCodeProvider, *, model_id=None, tools=None):
    events = []
    async for event in provider.stream(
        messages=[ProviderMessage("user", [TextBlock("hi")])],
        system="be brief",
        tools=tools,
        model_id=model_id,
    ):
        events.append(event)
    return events


def stop_of(events) -> MessageStopEvent:
    return next(e for e in events if isinstance(e, MessageStopEvent))


# --- OpenAI-compatible -------------------------------------------------------

async def test_openai_text_stream():
    chunks = [
        data_frame({"model": "m1", "choices": [{"delta": {"content": "Hel"}, "finish_reason": None}]}),
        data_frame({"model": "m1", "choices": [{"delta": {"content": "lo"}, "finish_reason": None}]}),
        data_frame(
            {
                "model": "m1",
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            }
        ),
        data_frame("[DONE]"),
    ]
    provider = make_provider(lambda request: httpx.Response(200, stream=_SSEStream(chunks)))
    events = await collect(provider, model_id="open-model")
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Hello"
    stop = stop_of(events)
    assert stop.stop_reason == "end_turn"
    assert stop.turn.text == "Hello"
    assert stop.turn.usage.input_tokens == 3
    assert stop.turn.usage.output_tokens == 2


async def test_openai_fragmented_tool_call_arguments():
    chunks = [
        data_frame(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "id": "call_1", "function": {"name": "demo__add", "arguments": '{"a":'}}
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            }
        ),
        data_frame(
            {
                "choices": [
                    {
                        "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '2,"b":3}'}}]},
                        "finish_reason": None,
                    }
                ]
            }
        ),
        data_frame({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
        data_frame("[DONE]"),
    ]
    provider = make_provider(lambda request: httpx.Response(200, stream=_SSEStream(chunks)))
    events = await collect(provider, model_id="open-model")

    starts = [e for e in events if isinstance(e, ToolCallStartEvent)]
    assert starts and starts[0].id == "call_1" and starts[0].name == "demo__add"
    assert any(isinstance(e, ToolCallDeltaEvent) for e in events)

    stop = stop_of(events)
    assert stop.stop_reason == "tool_use"
    calls = stop.turn.tool_calls
    assert len(calls) == 1
    assert isinstance(calls[0], ToolCallBlock)
    assert calls[0].arguments == {"a": 2, "b": 3}


async def test_openai_incomplete_tool_call_raises():
    chunks = [
        data_frame(
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "x", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}]}
        ),
        data_frame("[DONE]"),
    ]
    provider = make_provider(lambda request: httpx.Response(200, stream=_SSEStream(chunks)))
    with pytest.raises(ProviderError):
        await collect(provider, model_id="open-model")


async def test_openai_malformed_frame_raises():
    chunks = [data_frame("{not valid json"), data_frame("[DONE]")]
    provider = make_provider(lambda request: httpx.Response(200, stream=_SSEStream(chunks)))
    with pytest.raises(ProviderError):
        await collect(provider, model_id="open-model")


async def test_openai_length_maps_to_max_tokens():
    chunks = [
        data_frame({"choices": [{"delta": {"content": "cut"}, "finish_reason": "length"}]}),
        data_frame("[DONE]"),
    ]
    provider = make_provider(lambda request: httpx.Response(200, stream=_SSEStream(chunks)))
    stop = stop_of(await collect(provider, model_id="open-model"))
    assert stop.stop_reason == "max_tokens"


async def test_openai_stream_options_fallback():
    """A 400 complaining about stream_options triggers one retry without it."""
    seen_payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen_payloads.append(payload)
        if "stream_options" in payload:
            return httpx.Response(400, content=b'{"error":"unsupported parameter: stream_options"}')
        return httpx.Response(
            200,
            stream=_SSEStream(
                [
                    data_frame({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}),
                    data_frame("[DONE]"),
                ]
            ),
        )

    provider = make_provider(handler)
    events = await collect(provider, model_id="open-model")
    assert stop_of(events).turn.text == "ok"
    assert len(seen_payloads) == 2
    assert "stream_options" in seen_payloads[0]
    assert "stream_options" not in seen_payloads[1]


# --- Anthropic-compatible ----------------------------------------------------

async def test_anthropic_text_stream():
    chunks = [
        event_frame("message_start", {"message": {"model": "claude-x", "usage": {"input_tokens": 5}}}),
        event_frame("content_block_start", {"index": 0, "content_block": {"type": "text"}}),
        event_frame("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "Hi"}}),
        event_frame("content_block_stop", {"index": 0}),
        event_frame("message_delta", {"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}}),
        event_frame("message_stop", {}),
    ]
    provider = make_provider(
        lambda request: httpx.Response(200, stream=_SSEStream(chunks)),
        commandcode_api_style="anthropic",
    )
    events = await collect(provider, model_id="claude-x")
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Hi"
    stop = stop_of(events)
    assert stop.stop_reason == "end_turn"
    assert stop.turn.usage.input_tokens == 5
    assert stop.turn.usage.output_tokens == 1
    assert stop.turn.model_id == "claude-x"


async def test_anthropic_tool_use_stream():
    chunks = [
        event_frame("message_start", {"message": {"usage": {"input_tokens": 2}}}),
        event_frame(
            "content_block_start",
            {"index": 0, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "demo__add"}},
        ),
        event_frame("content_block_delta", {"index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"a":2,'}}),
        event_frame("content_block_delta", {"index": 0, "delta": {"type": "input_json_delta", "partial_json": '"b":3}'}}),
        event_frame("content_block_stop", {"index": 0}),
        event_frame("message_delta", {"delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 4}}),
        event_frame("message_stop", {}),
    ]
    provider = make_provider(
        lambda request: httpx.Response(200, stream=_SSEStream(chunks)),
        commandcode_api_style="anthropic",
    )
    events = await collect(provider, model_id="claude-x")
    assert any(isinstance(e, ToolCallStartEvent) for e in events)
    stop = stop_of(events)
    assert stop.stop_reason == "tool_use"
    assert stop.turn.tool_calls[0].arguments == {"a": 2, "b": 3}


def test_auto_style_routing():
    openai_provider = make_provider(lambda r: httpx.Response(200), commandcode_api_style="auto")
    assert openai_provider._resolve_style("deepseek/deepseek-v4-flash") == "openai"
    assert openai_provider._resolve_style("claude-3-5-sonnet") == "anthropic"
    assert openai_provider._resolve_style("anthropic/claude-x") == "anthropic"


# --- errors / auth / catalogue ----------------------------------------------

async def test_http_error_is_precise_and_redacted():
    def handler(request: httpx.Request) -> httpx.Response:
        # A hostile provider echoing the key must not leak it into the error text.
        return httpx.Response(401, content=b'{"error":"invalid api key test-key-123"}')

    provider = make_provider(handler)
    with pytest.raises(ProviderError) as excinfo:
        await collect(provider, model_id="open-model")
    message = str(excinfo.value)
    assert "401" in message
    assert "test-key-123" not in message


async def test_missing_key_raises_clearly():
    provider = CommandCodeProvider(make_settings(commandcode_api_key=None))
    with pytest.raises(ProviderError) as excinfo:
        await collect(provider, model_id="open-model")
    assert "COMMANDCODE_API_KEY" in str(excinfo.value)


async def test_authorization_header_sent():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, stream=_SSEStream([data_frame("[DONE]")]))

    provider = make_provider(handler)
    await collect(provider, model_id="open-model")
    assert captured[0].headers["authorization"] == "Bearer test-key-123"


async def test_anthropic_auth_headers_and_zdr():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, stream=_SSEStream([]))

    provider = make_provider(
        handler, commandcode_api_style="anthropic", commandcode_zdr=1
    )
    await collect(provider, model_id="claude-x")
    headers = captured[0].headers
    assert headers["x-api-key"] == "test-key-123"
    assert headers["anthropic-version"]
    assert headers["x-cmd-zdr"] == "1"


async def test_list_models_normalization():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models")
        return httpx.Response(
            200, json={"data": [{"id": "deepseek/deepseek-v4-flash", "name": "DeepSeek"}]}
        )

    provider = make_provider(handler)
    models = await provider.list_models()
    assert models[0].id == "deepseek/deepseek-v4-flash"
    assert models[0].label == "DeepSeek"


async def test_tools_are_converted_to_wire_shape():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, stream=_SSEStream([data_frame("[DONE]")]))

    provider = make_provider(handler)
    await collect(
        provider,
        model_id="open-model",
        tools=[ToolSpec(name="demo__add", description="add", input_schema={"type": "object"})],
    )
    payload = json.loads(captured[0].content)
    assert payload["tools"][0]["function"]["name"] == "demo__add"
    assert payload["tools"][0]["type"] == "function"
