from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Run, ToolAttempt
from backend.models.tool_attempt import PENDING, RECONCILED, SETTLED, UNCERTAIN
from backend.utils import utcnow


class ToolAttemptRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(
        self,
        run_id: str,
        *,
        tool_call_id: str,
        namespaced_name: str,
        arguments_digest: str,
        effect: str,
    ) -> ToolAttempt:
        attempt = ToolAttempt(
            run_id=run_id,
            tool_call_id=tool_call_id,
            namespaced_name=namespaced_name,
            arguments_digest=arguments_digest,
            effect=effect,
            status=PENDING,
        )
        self.session.add(attempt)
        self.session.flush()
        return attempt

    def get(self, attempt_id: str) -> ToolAttempt | None:
        return self.session.get(ToolAttempt, attempt_id)

    def set_status(
        self,
        attempt: ToolAttempt,
        status: str,
        *,
        is_error: bool | None = None,
        result_digest: str | None = None,
    ) -> ToolAttempt:
        attempt.status = status
        if is_error is not None:
            attempt.is_error = is_error
        if result_digest is not None:
            attempt.result_digest = result_digest
        if status in SETTLED or status == "unknown":
            attempt.ended_at = utcnow()
        self.session.flush()
        return attempt

    def reconcile_orphans(self) -> int:
        """Turn attempts left mid-flight by a previous process into unknowns.

        A row still ``pending`` or ``running`` means the process stopped between
        recording the dispatch and recording its result, so whether the side effect
        happened is genuinely unknown. Saying so is the only honest option.
        """
        stmt = select(ToolAttempt).where(ToolAttempt.status.in_(UNCERTAIN - {"unknown"}))
        orphans = list(self.session.execute(stmt).scalars().all())
        for attempt in orphans:
            attempt.status = "unknown"
            attempt.ended_at = utcnow()
        self.session.flush()
        return len(orphans)

    def uncertain_for_conversation(self, conversation_id: str) -> list[ToolAttempt]:
        stmt = (
            select(ToolAttempt)
            .join(Run, ToolAttempt.run_id == Run.id)
            .where(Run.conversation_id == conversation_id, ToolAttempt.status.in_(UNCERTAIN))
            .order_by(ToolAttempt.started_at.asc())
        )
        return list(self.session.execute(stmt).scalars().all())

    def find_uncertain(
        self, conversation_id: str, namespaced_name: str, arguments_digest: str
    ) -> ToolAttempt | None:
        stmt = (
            select(ToolAttempt)
            .join(Run, ToolAttempt.run_id == Run.id)
            .where(
                Run.conversation_id == conversation_id,
                ToolAttempt.namespaced_name == namespaced_name,
                ToolAttempt.arguments_digest == arguments_digest,
                ToolAttempt.status.in_(UNCERTAIN),
            )
            .order_by(ToolAttempt.started_at.desc())
        )
        return self.session.execute(stmt).scalars().first()

    def settled_mutating_for_conversation(
        self, conversation_id: str, *, limit: int = 20
    ) -> list[ToolAttempt]:
        """Completed calls that changed something, for the resume summary.

        Reads are excluded: they are cheap to repeat and listing them would bury the
        mutations that must not be repeated.
        """
        stmt = (
            select(ToolAttempt)
            .join(Run, ToolAttempt.run_id == Run.id)
            .where(
                Run.conversation_id == conversation_id,
                ToolAttempt.status == "succeeded",
                ToolAttempt.effect != "read_only",
            )
            .order_by(ToolAttempt.started_at.desc())
            .limit(limit)
        )
        return list(self.session.execute(stmt).scalars().all())

    def mark_reconciled(self, attempt: ToolAttempt) -> ToolAttempt:
        attempt.status = RECONCILED
        attempt.ended_at = utcnow()
        self.session.flush()
        return attempt
