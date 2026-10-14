from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SecretIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1)
    description: str = ""


class SkillToggle(BaseModel):
    enabled: bool


class SettingsIn(BaseModel):
    system_prompt: str | None = None
    unlimited_tools: bool | None = None


class AttachmentRef(BaseModel):
    path: str


class SendMessageWithAttachments(BaseModel):
    message: str = Field(min_length=0, default="")
    model_id: str | None = None
    attachments: list[str] = Field(default_factory=list)

    def model_post_init(self, _context: Any) -> None:  # noqa: D105
        if not self.message and not self.attachments:
            raise ValueError("message or attachments required")
