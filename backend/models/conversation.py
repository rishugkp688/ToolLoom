from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db import Base
from backend.utils import new_id, utcnow


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title_source: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    title_usage_input: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    title_usage_output: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    title_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    title_last_attempt: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    title_error: Mapped[str | None] = mapped_column(String(32), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    messages: Mapped[list["Message"]] = relationship(  # noqa: F821
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )
