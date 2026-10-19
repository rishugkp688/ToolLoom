"""Tool-output transformation and the execution-result container.

Tool observations can be large. We expose a bounded head+tail view to the model
(preserving error lines and stack traces at the tail) while the full text is
still available for the UI. As a later phase, the complete output is spilled to
the conversation workspace and the model is given a path to retrieve it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_MAX_CHARS = 40_000
_HEAD_RATIO = 0.7


@dataclass
class ExecutionResult:
    tool_call_id: str
    name: str
    input: dict[str, Any] = field(default_factory=dict)
    is_error: bool = False
    output_text: str = ""
    model_text: str = ""
    attempts: int = 1
    duration_ms: int = 0
    validation_errors: list[str] = field(default_factory=list)
    truncated: bool = False
    # The process exit status, kept separate from `is_error`. A command that ran and
    # exited non-zero is a delivered result, not a broken transport call.
    exit_code: int | None = None
    # Set when the command ran but its verification role failed (non-zero exit).
    verification_failed: bool = False
    # True when the failure happened after dispatch and cannot be proven not to have
    # taken effect (a timeout, a dropped response, a teardown error). The ledger
    # records these as "unknown" rather than "failed", so a repeat is not blind.
    outcome_uncertain: bool = False
    # Set when the caller already wrote this result into the transcript in the same
    # transaction as its settlement, so nobody writes it twice.
    persisted: bool = False
    attempt_id: str | None = None


def truncate_head_tail(
    text: str, max_chars: int = DEFAULT_MAX_CHARS, head_ratio: float = _HEAD_RATIO
) -> tuple[str, bool]:
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    head_len = int(max_chars * head_ratio)
    tail_len = max_chars - head_len
    omitted = len(text) - max_chars
    marker = f"\n\n…[truncated {omitted} chars]…\n\n"
    return text[:head_len] + marker + text[-tail_len:], True


def transform(name: str, raw: str, max_chars: int = DEFAULT_MAX_CHARS) -> tuple[str, bool]:
    """Return ``(model_text, truncated)`` for a tool output."""
    return truncate_head_tail(raw, max_chars=max_chars)


def to_result(call, raw: str, *, is_error: bool, attempts: int = 1, duration_ms: int = 0,
              max_chars: int = DEFAULT_MAX_CHARS) -> ExecutionResult:
    model_text, truncated = transform(call.name, raw, max_chars=max_chars)
    return ExecutionResult(
        tool_call_id=call.id,
        name=call.name,
        input=dict(call.arguments or {}),
        is_error=is_error,
        output_text=raw,
        model_text=model_text,
        attempts=attempts,
        duration_ms=duration_ms,
        truncated=truncated,
    )
