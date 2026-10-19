from __future__ import annotations

from pydantic import ConfigDict, Field, model_validator

from backend.schemas.safe import SafeModel


class CreateConversationRequest(SafeModel):
    # Unknown fields are rejected rather than ignored: a client that sends
    # {"message": ...} here should get an error, not an empty conversation.
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=255)


class SendMessageRequest(SafeModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=0, max_length=200_000, default="")
    model_id: str | None = None
    attachments: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def _require_content(self) -> SendMessageRequest:
        if not self.message.strip() and not self.attachments:
            raise ValueError("message or attachments required")
        return self


class CreateMCPServerRequest(SafeModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)
    # `local` and `generated` are runtime-owned; the registry rejects those with a
    # clearer message than a pattern failure would give.
    transport: str = Field(default="http", pattern="^(http|sse|local|stdio|generated)$")
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = False
    trusted: bool = False
    verify_ssl: bool = True
    timeout_ms: int = Field(default=30_000, ge=1_000, le=600_000)
    # stdio only: a fixed executable + arguments, never model-generated.
    command: str | None = None
    args: list[str] | None = None
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)


class UpdateMCPServerRequest(SafeModel):
    # Strict: a typo such as {"verify_ssl"} must fail rather than silently leave a
    # security setting unchanged while reporting success.
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    url: str | None = None
    headers: dict[str, str] | None = None
    verify_ssl: bool | None = None
    timeout_ms: int | None = Field(default=None, ge=1_000, le=600_000)


class UpdateToolRequest(SafeModel):
    # Strict: {"enable": true} (a typo for enabled) must not return 200 and quietly
    # leave a tool disabled or unapproved.
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    approved: bool | None = None
    risk: str | None = Field(default=None, pattern="^(low|medium|high)$")
    # Operator-owned effect classification. `unknown` is the safe default: no blind
    # retry after an uncertain outcome, and no concurrency.
    effect: str | None = Field(default=None, pattern="^(read_only|idempotent|mutating|unknown)$")
