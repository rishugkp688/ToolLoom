from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base
from backend.utils import utcnow

GLOBAL_SETTING_ID = "global"


class Setting(Base):
    __tablename__ = "settings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=GLOBAL_SETTING_ID)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    unlimited_tools: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )
