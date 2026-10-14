"""Deterministic, offline provider used by the test suite.

It never touches the network. Tests can either rely on the default echo
behavior or inject a ``script`` of turns (a :class:`ModelTurn` or a callable
``(messages, tools) -> ModelTurn``) to script multi-step tool-use flows.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable

from backend.services.providers.base import (
    BaseProvider,
    MessageStopEvent,
    ModelInfo,
    ModelTurn,
    ProviderMessage,
    TextBlock,
    TextDelta,
    ToolCallBlock,
    ToolCallDeltaEvent,
    ToolCallStartEvent,
    ToolSpec,
    Usage,
    UsageEvent,
)

ScriptItem = ModelTurn | Callable[[list[ProviderMessage], list[ToolSpec]], ModelTurn]


def _estimate_tokens(messages: list[ProviderMessage]) -> int:
    chars = sum(len(m.text) for m in messages)
    return max(1, chars // 4)


def _chunk_text(text: str) -> list[str]:
    """Split text into small whitespace-preserving chunks for streaming."""
    if not text:
        return [""]
    parts = text.split(" ")
    out: list[str] = []
    for i, part in enumerate(parts):
        out.append(part if i == 0 else " " + part)
    return out


class FakeProvider(BaseProvider):
    name = "fake"

    def __init__(self, script: list[ScriptItem] | None = None, model_id: str = "fake-echo") -> None:
        self.model_id = model_id
        self._script: list[ScriptItem] = list(script or [])
        self._cursor = 0

    # -- scripting helpers ----------------------------------------------------

    def _next_turn(
        self, messages: list[ProviderMessage], tools: list[ToolSpec]
    ) -> ModelTurn:
        if self._cursor < len(self._script):
            item = self._script[self._cursor]
            self._cursor += 1
            return item(messages, tools) if callable(item) else item
        return self._default_turn(messages)

    def _default_turn(self, messages: list[ProviderMessage]) -> ModelTurn:
        last_user = ""
        for message in reversed(messages):
            if message.role == "user" and message.text:
                last_user = message.text
                break
        text = f"Echo: {last_user}" if last_user else "Echo."
        usage = Usage(input_tokens=_estimate_tokens(messages), output_tokens=max(1, len(text) // 4))
        return ModelTurn([TextBlock(text=text)], "end_turn", usage, self.model_id)

    # -- provider interface ---------------------------------------------------

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
        turn = self._next_turn(messages, tools or [])
        tool_index = 0
        for block in turn.blocks:
            if isinstance(block, TextBlock):
                for chunk in _chunk_text(block.text):
                    await asyncio.sleep(0)
                    yield TextDelta(chunk)
            elif isinstance(block, ToolCallBlock):
                yield ToolCallStartEvent(index=tool_index, id=block.id, name=block.name)
                yield ToolCallDeltaEvent(
                    index=tool_index, arguments_delta=json.dumps(block.arguments)
                )
                tool_index += 1
        if turn.usage.total_tokens or turn.usage.input_tokens:
            yield UsageEvent(usage=turn.usage)
        yield MessageStopEvent(stop_reason=turn.stop_reason, turn=turn)

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(id=self.model_id, label="Fake (offline deterministic)")]
