from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db import Base
from backend.utils import new_id, utcnow


class MCPServer(Base):
    __tablename__ = "mcp_servers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # http | sse | local | stdio
    transport: Mapped[str] = mapped_column(String(16), nullable=False, default="http")
    url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # stdio transport: fixed executable + args, operator-configured only.
    command: Mapped[str | None] = mapped_column(String(512), nullable=True)
    args_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    env_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    cwd: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # JSON object of header name -> value; may contain ${secrets.NAME} references.
    headers_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    trusted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verify_ssl: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    timeout_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=30_000)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    tools: Mapped[list["Tool"]] = relationship(  # noqa: F821
        back_populates="server",
        cascade="all, delete-orphan",
    )
