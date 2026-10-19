from __future__ import annotations

from typing import Any

from pydantic import Field

from backend.schemas.safe import SafeModel


class SecretIn(SafeModel):
    name: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1)
    description: str = ""


class SkillToggle(SafeModel):
    enabled: bool


class SettingsIn(SafeModel):
    system_prompt: str | None = None
    unlimited_tools: bool | None = None


class AttachmentRef(SafeModel):
    path: str


class SendMessageWithAttachments(SafeModel):
    message: str = Field(min_length=0, default="")
    model_id: str | None = None
    attachments: list[str] = Field(default_factory=list)

    def model_post_init(self, _context: Any) -> None:  # noqa: D105
        if not self.message and not self.attachments:
            raise ValueError("message or attachments required")
