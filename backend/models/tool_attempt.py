"""One dispatch of one tool within a run, with its outcome state.

This is the row that answers "did that actually happen?". A side effect can land
just before the process dies, leaving no persisted result, so the record is written
**before** dispatch and settled afterwards, independently of the SSE stream.

The states:

* ``pending``    — recorded, not yet dispatched
* ``running``    — dispatch attempted
* ``succeeded``  — result confirmed
* ``failed``     — failure confirmed
* ``unknown``    — the process stopped between dispatch and result. The side effect
                   may or may not have happened.
* ``reconciled`` — a later read-only check resolved an unknown outcome

``effect`` is snapshotted at dispatch time rather than read from the tool row later,
so a decision is judged against the facts that held when it was made.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db import Base
from backend.utils import new_id, utcnow

PENDING = "pending"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
UNKNOWN = "unknown"
RECONCILED = "reconciled"

# An outcome we cannot vouch for.
UNCERTAIN = frozenset({PENDING, RUNNING, UNKNOWN})
# The dispatch finished, one way or another.
SETTLED = frozenset({SUCCEEDED, FAILED, RECONCILED})


class ToolAttempt(Base):
    __tablename__ = "tool_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    tool_call_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    namespaced_name: Mapped[str] = mapped_column(String(320), index=True, nullable=False)
    # sha256 of the canonical arguments, so an identical repeat is recognisable
    # without storing the arguments twice.
    arguments_digest: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # read_only | mutating | unknown, as it was when this attempt was made.
    effect: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=PENDING)
    # How many times this exact call has been dispatched.
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_error: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    result_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    run: Mapped["Run"] = relationship(back_populates="attempts")  # noqa: F821
