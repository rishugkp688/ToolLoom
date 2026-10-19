from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    provider: str
    model_id: str | None = None
    version: str


class ModelInfo(BaseModel):
    id: str
    label: str | None = None
    context_window: int | None = None
    effective_context_window: int | None = None
    context_source: str | None = None
    max_output_tokens: int | None = None


class ConversationSummary(BaseModel):
    id: str
    title: str | None = None
    title_source: str | None = None
    created_at: datetime
    updated_at: datetime
    message_count: int = 0


class MessageOut(BaseModel):
    id: str
    role: str
    content: str
    blocks: list[dict[str, Any]]
    user_text: str | None = None
    attachments: list[dict[str, Any]] = []
    tool_name: str | None = None
    tool_call_id: str | None = None
    created_at: datetime
    stop_reason: str | None = None


class ConversationDetail(ConversationSummary):
    messages: list[MessageOut]


class ToolOut(BaseModel):
    id: str
    server_id: str
    name: str
    namespaced_name: str
    description: str
    input_schema: dict[str, Any]
    enabled: bool
    risk: str = "low"
    approved: bool = False
    # Operator-owned effect classification: read_only | idempotent | mutating | unknown
    effect: str = "unknown"


class MCPServerOut(BaseModel):
    id: str
    name: str
    transport: str
    url: str | None = None
    header_names: list[str]
    enabled: bool
    trusted: bool
    verify_ssl: bool
    timeout_ms: int


class DiscoveryResponse(BaseModel):
    server_id: str
    added: int
    updated: int
    removed: int
    tools: list[ToolOut]


class ToolCallTrace(BaseModel):
    tool_call_id: str
    name: str
    input: dict[str, Any] = {}
    is_error: bool = False
    output_text: str = ""
    attempts: int = 1
    duration_ms: int = 0
    exit_code: int | None = None
    verification_failed: bool = False
    outcome_uncertain: bool = False
    attempt_id: str | None = None


class SendMessageResponse(BaseModel):
    reply: str
    iterations: int = 0
    stop_reason: str = "end_turn"
    usage: dict[str, int] = {}
    tool_calls: list[ToolCallTrace] = []


class UsageTotals(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost: float | None = None


class UsageResponse(BaseModel):
    global_: UsageTotals = UsageTotals()
    per_conversation: dict[str, UsageTotals] = {}
