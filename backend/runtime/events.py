"""Normalized events emitted by the agent runtime (SSE payloads)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentEvent:
    kind: str
    data: dict[str, Any] = field(default_factory=dict)


# Event kinds the frontend understands.
COMPACTING = "compacting"
COMPACTED = "compacted"
ITERATION_START = "iteration_start"
TOKEN = "token"
TOOL_CALL_START = "tool_call_start"
TOOL_CALL_VALIDATED = "tool_call_validated"
TOOL_CALL_RETRY = "tool_call_retry"
TOOL_CALL_END = "tool_call_end"
STEP_NOTE = "step_note"
USAGE = "usage"
DONE = "done"
ERROR = "error"
