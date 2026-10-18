from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db import Base
from backend.utils import new_id, utcnow

# How a tool affects the world, as classified by the operator rather than by the
# tool's own description. `unknown` is the default and the safe one: no blind retry,
# no concurrency.
#   read_only  — safe to repeat and safe to run alongside other reads
#   idempotent — repeating it changes nothing, but it must not run concurrently
#   mutating   — a side effect; repeating it may duplicate the effect
#   unknown    — unclassified; treated as mutating
EFFECTS = ("read_only", "idempotent", "mutating", "unknown")


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
    # Generated capabilities only: the package digest the operator approved. When a
    # reinstall changes it, approval is revoked, because the approval was for code
    # that is no longer installed.
    package_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # read_only | mutating | unknown. Operator-controlled, because a tool's own
    # description is a statement of intent rather than a guarantee: an MCP server
    # calling itself read-only is not evidence that it has no side effects.
    # `unknown` is the default so that anything unclassified stays sequential and is
    # never blindly retried. Drives concurrency safety and retry safety.
    effect: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    server: Mapped["MCPServer"] = relationship(back_populates="tools")  # noqa: F821
