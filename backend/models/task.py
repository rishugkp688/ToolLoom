"""What the agent is trying to accomplish.

Distinct from a conversation (what was said) and a run (one attempt at it):

* **Conversation** — the human and model transcript.
* **Task** — the objective, its plan, and what "done" means, across runs.
* **Run** — one execution attempt toward the task.

The original request is immutable; the objective and the plan are versioned, because
a user may revise requirements mid-task and a stale write must not silently overwrite
newer progress.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db import Base
from backend.utils import new_id, utcnow

ACTIVE = "active"
DONE = "done"
ABANDONED = "abandoned"


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # The user's first request for this task. Never rewritten: the model always sees
    # what was originally asked, even after the objective is revised.
    original_request: Mapped[str] = mapped_column(Text, nullable=False, default="")
    objective: Mapped[str] = mapped_column(Text, nullable=False, default="")
    objective_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    plan_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    plan_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    completion_criteria: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # active | done | abandoned
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=ACTIVE)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    runs: Mapped[list["Run"]] = relationship(  # noqa: F821
        back_populates="task", cascade="all, delete-orphan"
    )
