from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Run
from backend.models.run import INTERRUPTED, RUNNING, TERMINAL
from backend.utils import utcnow


class RunRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def start(
        self, conversation_id: str, *, task_id: str | None = None, resumed_from: str | None = None
    ) -> Run:
        run = Run(
            conversation_id=conversation_id,
            task_id=task_id,
            status=RUNNING,
            resumed_from_run_id=resumed_from,
        )
        self.session.add(run)
        self.session.flush()
        return run

    def get(self, run_id: str) -> Run | None:
        return self.session.get(Run, run_id)

    def finish(
        self,
        run: Run,
        *,
        status: str,
        stop_reason: str | None = None,
        error: str | None = None,
        iterations: int = 0,
        tool_calls: int = 0,
    ) -> Run:
        run.status = status
        run.stop_reason = stop_reason
        run.error = error
        run.iterations = iterations
        run.tool_calls = tool_calls
        run.ended_at = utcnow()
        self.session.flush()
        return run

    def last_for_conversation(self, conversation_id: str) -> Run | None:
        stmt = (
            select(Run)
            .where(Run.conversation_id == conversation_id)
            .order_by(Run.started_at.desc())
        )
        return self.session.execute(stmt).scalars().first()

    def list_for_conversation(self, conversation_id: str, *, limit: int = 20) -> list[Run]:
        stmt = (
            select(Run)
            .where(Run.conversation_id == conversation_id)
            .order_by(Run.started_at.desc())
            .limit(limit)
        )
        return list(self.session.execute(stmt).scalars().all())

    def last_settled(self, conversation_id: str) -> Run | None:
        """The most recent run that ended, whatever the outcome.

        This is what a resume reads: the record of the attempt that stopped, so the
        model learns why it stopped rather than being told to carry on blindly.
        """
        stmt = (
            select(Run)
            .where(Run.conversation_id == conversation_id, Run.status.in_(TERMINAL))
            .order_by(Run.ended_at.desc())
        )
        return self.session.execute(stmt).scalars().first()

    def reconcile_orphans(self) -> int:
        """Mark runs left ``running`` by a previous process as interrupted.

        Nothing else can distinguish "still working" from "the process died", and of
        the two, claiming the run is still active would be the dangerous guess.
        """
        stmt = select(Run).where(Run.status == RUNNING)
        orphans = list(self.session.execute(stmt).scalars().all())
        for run in orphans:
            run.status = INTERRUPTED
            run.error = "the process stopped before this run finished"
            if run.ended_at is None:
                run.ended_at = utcnow()
        self.session.flush()
        return len(orphans)
