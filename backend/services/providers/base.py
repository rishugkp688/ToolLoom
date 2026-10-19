"""Provider-neutral model contract.

The agent runtime, storage and UI only ever see these types. Every wire format
(OpenAI chat-completions, Anthropic Messages, …) is translated inside a
provider adapter — never here, and never in the runtime.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

StopReason = Literal[
    "end_turn",
    "tool_use",
    "max_tokens",
    "stop_sequence",
    "max_iterations",
    "max_tool_calls",
    "loop_guard",
    "cancelled",
    "error",
]


class ProviderError(RuntimeError):
    """Raised for provider/transport failures. Never swallowed silently."""

    def __init__(self, message: str, *, status: int = 502, category: str = "provider"):
        super().__init__(message)
        self.status = status
        self.category = category


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass
class TextBlock:
    text: str
    type: Literal["text"] = "text"


@dataclass
class ToolCallBlock:
    id: str
    name: str
    arguments: dict[str, Any]
    type: Literal["tool_use"] = "tool_use"
    # Set when the provider's argument string was not valid JSON. The call is still
    # surfaced so the model can be told to re-issue it, instead of the run dying.
    parse_error: str | None = None


@dataclass
class ToolResultBlock:
    tool_call_id: str
    content: str
    is_error: bool = False
    name: str | None = None
    exit_code: int | None = None
    verification_failed: bool | None = None
    outcome_uncertain: bool = False
    attempt_id: str | None = None
    type: Literal["tool_result"] = "tool_result"


Block = TextBlock | ToolCallBlock | ToolResultBlock


@dataclass
class ProviderMessage:
    role: Literal["user", "assistant"]
    blocks: list[Block] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(b.text for b in self.blocks if isinstance(b, TextBlock))


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class ModelTurn:
    blocks: list[Block]
    stop_reason: StopReason
    usage: Usage = field(default_factory=Usage)
    model_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "".join(b.text for b in self.blocks if isinstance(b, TextBlock))

    @property
    def tool_calls(self) -> list[ToolCallBlock]:
        return [b for b in self.blocks if isinstance(b, ToolCallBlock)]


@dataclass
class ModelInfo:
    id: str
    label: str | None = None
    context_window: int | None = None


# --- normalized stream events ------------------------------------------------


@dataclass
class TextDelta:
    text: str


@dataclass
class ToolCallStartEvent:
    index: int
    id: str
    name: str


@dataclass
class ToolCallDeltaEvent:
    index: int
    arguments_delta: str


@dataclass
class UsageEvent:
    usage: Usage


@dataclass
class MessageStopEvent:
    stop_reason: StopReason
    turn: ModelTurn


StreamEvent = TextDelta | ToolCallStartEvent | ToolCallDeltaEvent | UsageEvent | MessageStopEvent


# --- block (de)serialization for persistence ---------------------------------


def block_to_dict(block: Block) -> dict[str, Any]:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolCallBlock):
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "arguments": block.arguments,
        }
    return {
        "type": "tool_result",
        "tool_call_id": block.tool_call_id,
        "name": block.name,
        "content": block.content,
        "is_error": block.is_error,
        "exit_code": block.exit_code,
        "verification_failed": block.verification_failed,
        "outcome_uncertain": block.outcome_uncertain,
        "attempt_id": block.attempt_id,
    }


def block_from_dict(data: dict[str, Any]) -> Block:
    kind = data.get("type")
    if kind == "tool_use":
        return ToolCallBlock(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            arguments=data.get("arguments") or {},
        )
    if kind == "tool_result":
        return ToolResultBlock(
            tool_call_id=str(data.get("tool_call_id", "")),
            content=str(data.get("content", "")),
            is_error=bool(data.get("is_error", False)),
            name=data.get("name"),
            exit_code=data.get("exit_code"),
            verification_failed=data.get("verification_failed"),
            outcome_uncertain=bool(data.get("outcome_uncertain", False)),
            attempt_id=data.get("attempt_id"),
        )
    return TextBlock(text=str(data.get("text", "")))


def blocks_to_dicts(blocks: list[Block]) -> list[dict[str, Any]]:
    return [block_to_dict(b) for b in blocks]


def blocks_from_dicts(dicts: list[dict[str, Any]]) -> list[Block]:
    return [block_from_dict(d) for d in dicts if isinstance(d, dict)]


class BaseProvider(ABC):
    """Abstract provider. Subclasses implement :meth:`stream` and :meth:`list_models`."""

    name: str = "base"

    @abstractmethod
    def stream(
        self,
        *,
        messages: list[ProviderMessage],
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
        model_id: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[StreamEvent]:
        raise NotImplementedError

    @abstractmethod
    async def list_models(self) -> list[ModelInfo]:
        raise NotImplementedError

    async def complete(
        self, *, usage_observer: Callable[[Usage], None] | None = None, **kwargs: Any
    ) -> ModelTurn:
        turn: ModelTurn | None = None
        async for event in self.stream(**kwargs):
            if isinstance(event, UsageEvent) and usage_observer is not None:
                usage_observer(event.usage)
            if isinstance(event, MessageStopEvent):
                turn = event.turn
                if usage_observer is not None:
                    usage_observer(turn.usage)
        if turn is None:
            raise ProviderError("provider produced no message-stop event")
        return turn


def parse_arguments(raw: str) -> dict[str, Any]:
    """Parse a JSON argument string; raise ProviderError on malformed JSON."""
    raw = raw or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise ProviderError(f"malformed tool-call arguments: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ProviderError("tool-call arguments must be a JSON object")
    return parsed


def parse_arguments_safe(raw: str) -> tuple[dict[str, Any], str | None]:
    """Parse tool-call arguments without aborting the run.

    A model emitting invalid JSON is a *correctable* mistake, not a transport
    failure. Killing the run loses everything already done, so the call is surfaced
    with ``parse_error`` set and the runtime returns a corrective tool error — the
    model gets a chance to re-issue it.
    """
    text = (raw or "").strip() or "{}"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, f"arguments were not valid JSON ({exc.msg} at character {exc.pos})"
    if not isinstance(parsed, dict):
        return {}, "arguments must be a JSON object"
    return parsed, None
