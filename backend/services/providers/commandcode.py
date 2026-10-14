"""Command Code Provider API adapter.

Calls the documented endpoints (BUILD_SPEC.md §4):

    POST {base}/chat/completions   OpenAI-compatible chat (open models)
    POST {base}/messages           Anthropic-compatible Messages (Claude models)
    GET  {base}/models             model catalogue

All wire schemas live here. The agent runtime only ever sees the normalized
``ModelTurn`` / stream events from :mod:`backend.services.providers.base`.

Implemented with a well-tested ``httpx`` adapter rather than an SDK so no
provider SDK types leak into the runtime and so streaming can be parsed frame by
frame (including fragmented tool-call JSON).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from backend.services.providers.base import (
    BaseProvider,
    MessageStopEvent,
    ModelInfo,
    ModelTurn,
    ProviderError,
    ProviderMessage,
    TextBlock,
    TextDelta,
    ToolCallBlock,
    ToolCallDeltaEvent,
    ToolCallStartEvent,
    ToolResultBlock,
    ToolSpec,
    Usage,
    UsageEvent,
    parse_arguments,
)

_ANTHROPIC_VERSION = "2023-06-01"
_RETRYABLE_STATUS = {502, 503, 504}
_OPENAI_FINISH = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "max_tokens",
    "content_filter": "stop_sequence",
}
_ANTHROPIC_STOP = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "stop_sequence": "stop_sequence",
}


class CommandCodeProvider(BaseProvider):
    name = "commandcode"

    def __init__(self, settings, *, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.model_id = settings.commandcode_model
        self._client = client
        self._base = settings.commandcode_base_url.rstrip("/")

    # -- public interface -----------------------------------------------------

    async def list_models(self) -> list[ModelInfo]:
        data = await self._get_json(f"{self._base}/models")
        return self._normalize_models(data)

    async def stream(
        self,
        *,
        messages: list[ProviderMessage],
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
        model_id: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[object]:
        model = model_id or self.model_id
        style = self._resolve_style(model)
        if style == "anthropic":
            generator = self._stream_anthropic(
                messages, system, tools, model, max_tokens, temperature
            )
        else:
            generator = self._stream_openai(messages, system, tools, model, max_tokens, temperature)
        async for event in generator:
            yield event

    # -- style / auth ---------------------------------------------------------

    def _resolve_style(self, model: str) -> str:
        style = (self.settings.commandcode_api_style or "openai").lower()
        if style in {"openai", "anthropic"}:
            return style
        lowered = (model or "").lower()
        if "claude" in lowered or "anthropic" in lowered:
            return "anthropic"
        return "openai"

    def _require_key(self) -> str:
        key = self.settings.commandcode_api_key
        if not key:
            raise ProviderError(
                "COMMANDCODE_API_KEY is not set. Add it to your local .env to use "
                "LLM_PROVIDER=commandcode (or set LLM_PROVIDER=fake for offline mode)."
            )
        return key

    def _headers(self, style: str) -> dict[str, str]:
        key = self._require_key()
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if style == "anthropic":
            headers["x-api-key"] = key
            headers["anthropic-version"] = _ANTHROPIC_VERSION
        else:
            headers["Authorization"] = f"Bearer {key}"
        if self.settings.commandcode_zdr:
            headers["x-cmd-zdr"] = "1"
        return headers

    def _redact(self, text: str) -> str:
        key = self.settings.commandcode_api_key
        return text.replace(key, "***") if key else text

    # -- request plumbing -----------------------------------------------------

    async def _iter_sse(
        self, url: str, headers: dict[str, str], payload: dict[str, Any]
    ) -> AsyncIterator[tuple[str, str]]:
        """Yield ``(event_name, data)`` tuples, retrying only pre-response failures."""
        attempts = 0
        while True:
            attempts += 1
            owns_client = self._client is None
            client = self._client or httpx.AsyncClient(
                timeout=httpx.Timeout(connect=15.0, read=300.0, write=30.0, pool=15.0)
            )
            started = False
            try:
                async with client.stream("POST", url, headers=headers, json=payload) as response:
                    if response.status_code >= 400:
                        body = await response.aread()
                        detail = self._redact(body.decode("utf-8", "replace"))
                        error = ProviderError(
                            f"provider returned HTTP {response.status_code}: {detail[:500]}"
                        )
                        retryable = (
                            response.status_code in _RETRYABLE_STATUS
                            and attempts < 2
                            and not started
                        )
                        if retryable:
                            await asyncio.sleep(0.5)
                            continue
                        raise error
                    async for event in _sse_events(response):
                        started = True
                        yield event
                return
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
                if attempts < 2 and not started:
                    await asyncio.sleep(0.5)
                    continue
                raise ProviderError(self._redact(f"provider connection failed: {exc}")) from exc
            finally:
                if owns_client:
                    await client.aclose()

    async def _get_json(self, url: str) -> Any:
        headers = self._headers("openai")
        headers["Accept"] = "application/json"
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        try:
            response = await client.get(url, headers=headers)
            if response.status_code >= 400:
                raise ProviderError(
                    f"provider returned HTTP {response.status_code}: "
                    f"{self._redact(response.text)[:500]}"
                )
            return response.json()
        except httpx.HTTPError as exc:
            raise ProviderError(self._redact(f"provider request failed: {exc}")) from exc
        finally:
            if owns_client:
                await client.aclose()

    # -- OpenAI-compatible ----------------------------------------------------

    def _openai_payload(
        self,
        messages: list[ProviderMessage],
        system: str | None,
        tools: list[ToolSpec] | None,
        model: str,
        max_tokens: int | None,
        temperature: float | None,
        *,
        include_usage: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": _to_openai_messages(messages, system),
            "stream": True,
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema or {"type": "object", "properties": {}},
                    },
                }
                for tool in tools
            ]
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature
        if include_usage:
            payload["stream_options"] = {"include_usage": True}
        return payload

    async def _stream_openai(
        self,
        messages: list[ProviderMessage],
        system: str | None,
        tools: list[ToolSpec] | None,
        model: str,
        max_tokens: int | None,
        temperature: float | None,
    ) -> AsyncIterator[object]:
        url = f"{self._base}/chat/completions"
        headers = self._headers("openai")
        payloads = [
            self._openai_payload(
                messages, system, tools, model, max_tokens, temperature, include_usage=True
            ),
            self._openai_payload(
                messages, system, tools, model, max_tokens, temperature, include_usage=False
            ),
        ]

        last_error: ProviderError | None = None
        for payload in payloads:
            text_parts: list[str] = []
            tool_acc: dict[int, dict[str, str]] = {}
            started_indexes: set[int] = set()
            usage = Usage()
            finish_reason: str | None = None
            model_used = model
            yielded = False
            try:
                async for _event_name, data in self._iter_sse(url, headers, payload):
                    if data == "[DONE]":
                        break
                    chunk = self._parse_frame(data)
                    model_used = chunk.get("model") or model_used
                    if isinstance(chunk.get("usage"), dict):
                        usage = _usage_from_openai(chunk["usage"])
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta") or {}
                    content = delta.get("content")
                    if content:
                        text_parts.append(content)
                        yielded = True
                        yield TextDelta(content)
                    for tool_delta in delta.get("tool_calls") or []:
                        index = int(tool_delta.get("index", 0))
                        acc = tool_acc.setdefault(index, {"id": "", "name": "", "arguments": ""})
                        if tool_delta.get("id"):
                            acc["id"] = tool_delta["id"]
                        function = tool_delta.get("function") or {}
                        if function.get("name"):
                            acc["name"] += function["name"]
                        if index not in started_indexes and acc["id"] and acc["name"]:
                            started_indexes.add(index)
                            yield ToolCallStartEvent(index=index, id=acc["id"], name=acc["name"])
                        if function.get("arguments"):
                            acc["arguments"] += function["arguments"]
                            yielded = True
                            yield ToolCallDeltaEvent(
                                index=index, arguments_delta=function["arguments"]
                            )
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                turn = self._build_openai_turn(
                    text_parts, tool_acc, finish_reason, usage, model_used
                )
                if turn.usage.total_tokens or turn.usage.input_tokens:
                    yield UsageEvent(turn.usage)
                yield MessageStopEvent(stop_reason=turn.stop_reason, turn=turn)
                return
            except ProviderError as exc:
                last_error = exc
                message = str(exc)
                if not yielded and "stream_options" in message:
                    continue  # provider rejects include_usage — retry without it
                raise
        if last_error is not None:
            raise last_error

    def _parse_frame(self, data: str) -> dict[str, Any]:
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ProviderError(f"malformed response frame: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ProviderError("unexpected response frame type")
        return parsed

    def _build_openai_turn(
        self,
        text_parts: list[str],
        tool_acc: dict[int, dict[str, str]],
        finish_reason: str | None,
        usage: Usage,
        model: str,
    ) -> ModelTurn:
        blocks: list = []
        text = "".join(text_parts)
        if text:
            blocks.append(TextBlock(text=text))
        for index in sorted(tool_acc):
            acc = tool_acc[index]
            if not acc["id"] or not acc["name"]:
                raise ProviderError("provider emitted an incomplete tool call (missing id/name)")
            blocks.append(
                ToolCallBlock(
                    id=acc["id"],
                    name=acc["name"],
                    arguments=parse_arguments(acc["arguments"]),
                )
            )
        if finish_reason is None:
            stop = "error"
        else:
            stop = _OPENAI_FINISH.get(finish_reason, "stop_sequence")
        return ModelTurn(
            blocks=blocks,
            stop_reason=stop,
            usage=usage,
            model_id=model,
            metadata={"finish_reason": finish_reason},
        )

    # -- Anthropic-compatible -------------------------------------------------

    async def _stream_anthropic(
        self,
        messages: list[ProviderMessage],
        system: str | None,
        tools: list[ToolSpec] | None,
        model: str,
        max_tokens: int | None,
        temperature: float | None,
    ) -> AsyncIterator[object]:
        url = f"{self._base}/messages"
        headers = self._headers("anthropic")
        payload: dict[str, Any] = {
            "model": model,
            "messages": _to_anthropic_messages(messages),
            "max_tokens": max_tokens or self.settings.model_max_output_tokens,
            "stream": True,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema or {"type": "object", "properties": {}},
                }
                for tool in tools
            ]
        if temperature is not None:
            payload["temperature"] = temperature

        blocks: dict[int, dict[str, Any]] = {}
        order: list[int] = []
        args_acc: dict[int, str] = {}
        usage = Usage()
        stop_reason: str | None = None
        model_used = model

        async for event_name, data in self._iter_sse(url, headers, payload):
            if event_name == "ping":
                continue
            frame = self._parse_frame(data)
            if event_name == "error":
                raise ProviderError(f"provider error: {frame}")
            if event_name == "message_start":
                message = frame.get("message") or {}
                model_used = message.get("model") or model_used
                usage.input_tokens = int((message.get("usage") or {}).get("input_tokens") or 0)
            elif event_name == "content_block_start":
                index = int(frame.get("index", 0))
                block = frame.get("content_block") or {}
                btype = block.get("type")
                if index not in blocks:
                    order.append(index)
                if btype == "tool_use":
                    blocks[index] = {
                        "type": "tool_use",
                        "id": block.get("id", ""),
                        "name": block.get("name", ""),
                    }
                    args_acc[index] = ""
                    if block.get("id") and block.get("name"):
                        yield ToolCallStartEvent(index=index, id=block["id"], name=block["name"])
                else:
                    blocks[index] = {"type": "text", "parts": []}
            elif event_name == "content_block_delta":
                index = int(frame.get("index", 0))
                delta = frame.get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    entry = blocks.setdefault(index, {"type": "text", "parts": []})
                    entry.setdefault("parts", []).append(delta["text"])
                    yield TextDelta(delta["text"])
                elif delta.get("type") == "input_json_delta" and delta.get("partial_json"):
                    args_acc[index] = args_acc.get(index, "") + delta["partial_json"]
                    yield ToolCallDeltaEvent(index=index, arguments_delta=delta["partial_json"])
            elif event_name == "message_delta":
                delta = frame.get("delta") or {}
                if delta.get("stop_reason"):
                    stop_reason = delta["stop_reason"]
                out_tokens = (frame.get("usage") or {}).get("output_tokens")
                if out_tokens is not None:
                    usage.output_tokens = int(out_tokens)
            elif event_name == "message_stop":
                break

        model_blocks: list = []
        for index in order:
            entry = blocks.get(index) or {}
            if entry.get("type") == "tool_use":
                if not entry.get("id") or not entry.get("name"):
                    raise ProviderError(
                        "provider emitted an incomplete tool call (missing id/name)"
                    )
                model_blocks.append(
                    ToolCallBlock(
                        id=entry["id"],
                        name=entry["name"],
                        arguments=parse_arguments(args_acc.get(index, "")),
                    )
                )
            else:
                text = "".join(entry.get("parts") or [])
                if text:
                    model_blocks.append(TextBlock(text=text))

        if stop_reason is None:
            stop = "error"
        else:
            stop = _ANTHROPIC_STOP.get(stop_reason, "stop_sequence")
        turn = ModelTurn(
            blocks=model_blocks,
            stop_reason=stop,
            usage=usage,
            model_id=model_used,
            metadata={"stop_reason": stop_reason},
        )
        if usage.total_tokens or usage.input_tokens:
            yield UsageEvent(usage)
        yield MessageStopEvent(stop_reason=turn.stop_reason, turn=turn)

    # -- catalogue ------------------------------------------------------------

    def _normalize_models(self, data: Any) -> list[ModelInfo]:
        entries: list[Any] = []
        if isinstance(data, dict):
            for key in ("data", "models", "items"):
                if isinstance(data.get(key), list):
                    entries = data[key]
                    break
            else:
                entries = [data]
        elif isinstance(data, list):
            entries = data
        models: list[ModelInfo] = []
        for entry in entries:
            if isinstance(entry, str):
                models.append(ModelInfo(id=entry))
            elif isinstance(entry, dict):
                identifier = entry.get("id") or entry.get("name") or entry.get("model")
                if identifier:
                    label = entry.get("label") or entry.get("display_name")
                    if label is None and entry.get("name") not in (None, identifier):
                        label = entry.get("name")
                    window = entry.get("context_length")
                    if not isinstance(window, int) or isinstance(window, bool) or window < 512:
                        window = None
                    models.append(ModelInfo(id=str(identifier), label=label, context_window=window))
        return models


# --- helpers -----------------------------------------------------------------


async def _sse_events(response: httpx.Response) -> AsyncIterator[tuple[str, str]]:
    """Parse an SSE byte stream into ``(event, data)`` tuples."""
    event: str | None = None
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if data_lines:
                yield event or "message", "\n".join(data_lines)
            event = None
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].lstrip())
    if data_lines:
        yield event or "message", "\n".join(data_lines)


def _usage_from_openai(raw: dict[str, Any]) -> Usage:
    prompt = int(raw.get("prompt_tokens") or 0)
    completion = int(raw.get("completion_tokens") or 0)
    return Usage(input_tokens=prompt, output_tokens=completion)


def _to_openai_messages(messages: list[ProviderMessage], system: str | None) -> list[dict]:
    out: list[dict] = []
    if system:
        out.append({"role": "system", "content": system})
    for message in messages:
        if message.role == "assistant":
            tool_calls = [
                {
                    "id": block.id,
                    "type": "function",
                    "function": {"name": block.name, "arguments": json.dumps(block.arguments)},
                }
                for block in message.blocks
                if isinstance(block, ToolCallBlock)
            ]
            entry: dict[str, Any] = {"role": "assistant", "content": message.text or None}
            if tool_calls:
                entry["tool_calls"] = tool_calls
            out.append(entry)
            continue
        texts = "".join(b.text for b in message.blocks if isinstance(b, TextBlock))
        if texts:
            out.append({"role": "user", "content": texts})
        for block in message.blocks:
            if isinstance(block, ToolResultBlock):
                out.append(
                    {"role": "tool", "tool_call_id": block.tool_call_id, "content": block.content}
                )
    return out


def _to_anthropic_messages(messages: list[ProviderMessage]) -> list[dict]:
    out: list[dict] = []
    for message in messages:
        content: list[dict] = []
        for block in message.blocks:
            if isinstance(block, TextBlock):
                content.append({"type": "text", "text": block.text})
            elif isinstance(block, ToolCallBlock):
                content.append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.arguments,
                    }
                )
            elif isinstance(block, ToolResultBlock):
                content.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.tool_call_id,
                        "content": block.content,
                        "is_error": block.is_error,
                    }
                )
        if content:
            out.append({"role": message.role, "content": content})
    return out
