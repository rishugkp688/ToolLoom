from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db import Base
from backend.utils import new_id, utcnow


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Display data is separate from the full model-facing attachment preview.
    user_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachments_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    # Provider-neutral blocks (TextBlock / ToolCallBlock) as JSON — authoritative.
    content_blocks_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    tool_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_resume: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    usage_input: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    usage_output: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stop_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, index=True, nullable=False
    )

    conversation: Mapped["Conversation"] = relationship(  # noqa: F821
        back_populates="messages"
    )
