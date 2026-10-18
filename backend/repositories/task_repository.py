from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Task
from backend.models.task import ACTIVE
from backend.utils import utcnow


class TaskRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, conversation_id: str, *, original_request: str = "") -> Task:
        task = Task(
            conversation_id=conversation_id,
            original_request=original_request,
            objective=original_request,
        )
        self.session.add(task)
        self.session.flush()
        return task

    def get(self, task_id: str) -> Task | None:
        return self.session.get(Task, task_id)

    def active_for_conversation(self, conversation_id: str) -> Task | None:
        """The task a new run belongs to, creating one when the request changes.

        A conversation can hold several tasks over its life: a user who pivots to a
        different piece of work starts a new one, and the previous task is left
        behind as history rather than being rewritten.
        """
        stmt = (
            select(Task)
            .where(Task.conversation_id == conversation_id, Task.status == ACTIVE)
            .order_by(Task.created_at.desc())
        )
        return self.session.execute(stmt).scalars().first()

    def list_for_conversation(self, conversation_id: str) -> list[Task]:
        stmt = (
            select(Task)
            .where(Task.conversation_id == conversation_id)
            .order_by(Task.created_at.asc())
        )
        return list(self.session.execute(stmt).scalars().all())

    def revise(
        self,
        task: Task,
        *,
        objective: str | None = None,
        plan: list | None = None,
        completion_criteria: str | None = None,
    ) -> Task:
        """Apply a revision, bumping only the revisions that actually moved.

        Each field carries its own revision so a stale write cannot silently
        overwrite newer progress, and the original request is never touched.
        """
        if objective is not None and objective != task.objective:
            task.objective = objective
            task.objective_revision += 1
        if plan is not None:
            encoded = json.dumps(plan)
            if encoded != task.plan_json:
                task.plan_json = encoded
                task.plan_revision += 1
        if completion_criteria is not None and completion_criteria != task.completion_criteria:
            task.completion_criteria = completion_criteria
        task.updated_at = utcnow()
        self.session.flush()
        return task

    def set_status(self, task: Task, status: str) -> Task:
        task.status = status
        task.updated_at = utcnow()
        self.session.flush()
        return task

    def plan_of(self, task: Task) -> list:
        try:
            plan = json.loads(task.plan_json or "[]")
        except json.JSONDecodeError:
            return []
        return plan if isinstance(plan, list) else []
