from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CreateConversationRequest(BaseModel):
    title: str | None = Field(default=None, max_length=255)


class SendMessageRequest(BaseModel):
    message: str = Field(min_length=0, max_length=200_000, default="")
    model_id: str | None = None
    attachments: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def _require_content(self) -> SendMessageRequest:
        if not self.message.strip() and not self.attachments:
            raise ValueError("message or attachments required")
        return self


class CreateMCPServerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)
    transport: str = Field(default="http", pattern="^(http|sse|local|stdio)$")
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


class UpdateMCPServerRequest(BaseModel):
    enabled: bool | None = None
    url: str | None = None
    headers: dict[str, str] | None = None
    verify_ssl: bool | None = None
    timeout_ms: int | None = Field(default=None, ge=1_000, le=600_000)


class UpdateToolRequest(BaseModel):
    enabled: bool | None = None
    approved: bool | None = None
    risk: str | None = Field(default=None, pattern="^(low|medium|high)$")
