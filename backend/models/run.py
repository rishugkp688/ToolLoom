"""One execution attempt toward a task.

The runtime is request-owned, so a run is not a background job: this row is the
durable *record* of an attempt, created when the attempt starts and closed whatever
way it ends. It is what makes "what happened last time?" answerable after a crash,
a timeout or a closed tab, and what a resume reads instead of guessing.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db import Base
from backend.utils import new_id, utcnow

RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
INTERRUPTED = "interrupted"
CANCELLED = "cancelled"

# Statuses that mean the process stopped without choosing to.
UNSETTLED = frozenset({RUNNING})
TERMINAL = frozenset({COMPLETED, FAILED, INTERRUPTED, CANCELLED})


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    task_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tasks.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # running | completed | failed | interrupted | cancelled
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=RUNNING)
    stop_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Why it stopped, in the operator's words: a limit name, an exception class or
    # an interruption notice.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    iterations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Set when this attempt continues an earlier one, so a chain is traceable.
    resumed_from_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    task: Mapped["Task | None"] = relationship(back_populates="runs")  # noqa: F821
    attempts: Mapped[list["ToolAttempt"]] = relationship(  # noqa: F821
        back_populates="run", cascade="all, delete-orphan"
    )
