from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db import Base
from backend.utils import new_id, utcnow


class Tool(Base):
    __tablename__ = "tools"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    server_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("mcp_servers.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    namespaced_name: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    input_schema_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # low | medium | high. High-risk tools also need explicit operator approval.
    risk: Mapped[str] = mapped_column(String(16), nullable=False, default="low")
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    server: Mapped["MCPServer"] = relationship(back_populates="tools")  # noqa: F821
