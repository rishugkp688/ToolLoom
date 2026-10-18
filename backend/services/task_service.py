"""Task state: the durable objective, plan and evidence that outlive one run.

The task record is the single authority. ``plan.md`` in the workspace is a
*projection* written from it, so a model editing files directly cannot create a
second, competing version of the truth.

The rendered blocks below go into the system prompt, which places them outside the
message-trimming path: the objective and the plan cannot be summarised away, however
long a run gets.
"""

from __future__ import annotations

from pathlib import Path

from backend.models.task import ACTIVE, Task
from backend.repositories import TaskRepository

MAX_BLOCK_CHARS = 4_000
PLAN_STATUSES = ("pending", "in_progress", "completed")


class TaskService:
    def __init__(self, db, settings) -> None:
        self.db = db
        self.settings = settings

    def active(self, conversation_id: str) -> Task | None:
        with self.db.session() as session:
            task = TaskRepository(session).active_for_conversation(conversation_id)
            if task is None:
                return None
            session.expunge(task)
            return task

    def ensure(self, conversation_id: str, *, request: str = "") -> Task:
        """The task for this conversation, created on first use."""
        with self.db.session() as session:
            repository = TaskRepository(session)
            task = repository.active_for_conversation(conversation_id)
            if task is None:
                task = repository.create(conversation_id, original_request=request.strip())
            session.expunge(task)
            return task

    def revise(
        self,
        conversation_id: str,
        *,
        objective: str | None = None,
        plan: list | None = None,
        completion_criteria: str | None = None,
    ) -> Task:
        with self.db.session() as session:
            repository = TaskRepository(session)
            task = repository.active_for_conversation(conversation_id)
            if task is None:
                task = repository.create(conversation_id)
            repository.revise(
                task,
                objective=objective,
                plan=plan,
                completion_criteria=completion_criteria,
            )
            session.flush()
            session.expunge(task)
            return task

    def set_status(self, conversation_id: str, status: str) -> bool:
        with self.db.session() as session:
            repository = TaskRepository(session)
            task = repository.active_for_conversation(conversation_id)
            if task is None:
                return False
            repository.set_status(task, status)
            return True

    def plan_of(self, task: Task) -> list:
        with self.db.session() as session:
            return TaskRepository(session).plan_of(task)

    def finished(self, conversation_id: str) -> bool:
        task = self.active(conversation_id)
        return task is None or task.status != ACTIVE

    def render_task_block(self, conversation_id: str) -> str:
        """The pinned objective. Never trimmed, never summarised."""
        task = self.active(conversation_id)
        if task is None:
            return ""
        with self.db.session() as session:
            plan = TaskRepository(session).plan_of(task)
        lines = ["<task>"]
        if task.original_request:
            lines.append(f"original_request: {task.original_request.strip()}")
        lines.append(f"objective (rev {task.objective_revision}): {task.objective.strip()}")
        if task.completion_criteria:
            lines.append(f"completion_criteria: {task.completion_criteria.strip()}")
        if plan:
            lines.append(f"plan (rev {task.plan_revision}):")
            for index, item in enumerate(plan, start=1):
                step = str(item.get("step", "")).strip()
                status = str(item.get("status", "pending"))
                lines.append(f"{index}. [{status}] {step}")
        lines.append("</task>")
        return "\n".join(lines)[:MAX_BLOCK_CHARS]

    def render_execution_block(self, conversation_id: str, ledger) -> str:
        """Why the previous attempt stopped, and what must not be repeated blindly."""
        from backend.repositories import RunRepository

        with self.db.session() as session:
            run = RunRepository(session).last_settled(conversation_id)
            previous = (
                {
                    "status": run.status,
                    "stop_reason": run.stop_reason,
                    "error": run.error,
                    "iterations": run.iterations,
                }
                if run is not None
                else None
            )
        evidence = ledger.describe_conversation(conversation_id)
        if previous is None and not evidence["completed"] and not evidence["uncertain"]:
            return ""

        lines = ["<execution_state>"]
        if previous is not None:
            detail = previous["stop_reason"] or previous["error"] or "stopped"
            lines.append(
                f"previous_run: {previous['status']} after {previous['iterations']} "
                f"iteration(s) ({detail})"
            )
        if evidence["completed"]:
            lines.append("completed_changes (already done — do not repeat):")
            lines.extend(f"- {item['tool']}" for item in evidence["completed"])
        if evidence["uncertain"]:
            lines.append(
                "uncertain_outcomes (verify before repeating; the side effect may or may "
                "not have happened):"
            )
            lines.extend(f"- {item['tool']} ({item['status']})" for item in evidence["uncertain"])
        lines.append("</execution_state>")
        return "\n".join(lines)[:MAX_BLOCK_CHARS]

    def project_plan(self, workspace, conversation_id: str) -> None:
        """Write ``plan.md`` as a readable projection of the record.

        Best-effort by design: a projection that cannot be written must never fail a
        run, because the authoritative copy is in SQLite either way.
        """
        task = self.active(conversation_id)
        if task is None:
            return
        try:
            with self.db.session() as session:
                plan = TaskRepository(session).plan_of(task)
            body = [f"# {task.objective.strip() or 'Task'}", ""]
            if task.original_request.strip():
                body += [f"Original request: {task.original_request.strip()}", ""]
            if task.completion_criteria.strip():
                body += [f"Completion criteria: {task.completion_criteria.strip()}", ""]
            body.append(f"Plan (rev {task.plan_revision})")
            for index, item in enumerate(plan, start=1):
                status = str(item.get("status", "pending"))
                body.append(f"{index}. [{status}] {str(item.get('step', '')).strip()}")
            target = Path(workspace.root) / "plan.md"
            target.write_text("\n".join(body) + "\n", encoding="utf-8")
        except OSError:
            return
