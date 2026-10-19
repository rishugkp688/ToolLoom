"""Task state: the durable objective, plan and evidence that outlive one run.

The task record is the single authority. ``.toolloom/plan.md`` in the workspace is
a *projection* written from it, so a model editing files directly cannot create a
second, competing version of the truth. The runtime namespace keeps that projection
from clobbering an operator's own ``plan.md`` or ``request.md``.

The rendered blocks below go into the system prompt, which places them outside the
message-trimming path: the objective and the plan cannot be summarised away, however
long a run gets.
"""

from __future__ import annotations

from backend.models.task import ACTIVE, Task
from backend.repositories import TaskRepository

MAX_BLOCK_CHARS = 4_000
PLAN_STATUSES = ("pending", "in_progress", "completed")
# The task's own lifecycle. `active` is the only state the pinned block renders, so
# completing a task ends its authority over later runs.
TASK_STATUSES = ("active", "completed")


def _bound(text: str, budget: int) -> str:
    """Cut for display only, and say so. Never a silent trim."""
    if len(text) <= budget:
        return text
    return f"{text[:budget]}\n…[{len(text) - budget:,} characters not shown here]"


def _render_plan(plan: list, revision: int, budget: int) -> list[str]:
    """A bounded view of a plan: active/next steps first, completed summarised."""
    if not plan:
        return []
    header = f"plan (rev {revision}):"
    lines = [header]
    completed = [
        (index, str(item.get("step", "")).strip())
        for index, item in enumerate(plan, start=1)
        if str(item.get("status", "pending")) == "completed"
    ]
    if completed:
        last_index, last_step = completed[-1]
        lines.append(
            f"completed: {len(completed)} step(s); last: {last_index}. {last_step[:120]}"
        )
    remaining = budget - sum(len(line) + 1 for line in lines)
    omitted = 0
    for index, item in enumerate(plan, start=1):
        status = str(item.get("status", "pending"))
        if status == "completed":
            continue
        step = str(item.get("step", "")).strip()
        line = f"{index}. [{status}] {step[:200]}"
        # Reserve room for the omission pointer, so the model always learns that
        # the full plan is retrievable rather than silently seeing a partial one.
        if len(line) + 1 > remaining - 96:
            omitted += 1
            continue
        lines.append(line)
        remaining -= len(line) + 1
    if omitted:
        lines.append(
            f"…[{omitted} step(s) omitted; full plan at .toolloom/plan.md or plan_read]"
        )
    return lines


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

    def set_status(self, conversation_id: str, status: str) -> Task | None:
        if status not in TASK_STATUSES:
            raise ValueError(f"status must be one of {', '.join(TASK_STATUSES)}")
        with self.db.session() as session:
            repository = TaskRepository(session)
            task = repository.active_for_conversation(conversation_id)
            if task is None:
                return None
            repository.set_status(task, status)
            session.flush()
            # Return the updated object, not the caller's stale detached copy: the
            # completion tool used to report `active` after setting `completed`.
            session.expunge(task)
            return task

    def plan_of(self, task: Task) -> list:
        with self.db.session() as session:
            return TaskRepository(session).plan_of(task)

    def finished(self, conversation_id: str) -> bool:
        task = self.active(conversation_id)
        return task is None or task.status != ACTIVE

    MAX_RENDERED_REQUEST = 8_000

    def _request_for_prompt(self, task, conversation_id: str, budget: int) -> str:
        """The full original request, or as much as fits plus a pointer to all of it.

        The task record always keeps the whole thing. Only this *rendering* is
        bounded, and when it is cut the model is told so and given the path — the
        silent truncation is what let a task hold an objective that was not the one
        that was requested.
        """
        text = (task.original_request or "").strip()
        if not text:
            return ""
        if len(text) <= budget and len(text) <= self.MAX_RENDERED_REQUEST:
            return text
        reference = self._spill_request(text, conversation_id)
        if reference is None:
            omitted = max(0, len(text) - budget)
            return f"{text[:budget]}\n…[{omitted:,} characters omitted]"
        return (
            f"{text[:budget]}\n"
            f"…[{len(text) - budget:,} characters omitted; the complete request is at "
            f"`{reference}` — read it before assuming the requirement is complete]"
        )

    def _spill_request(self, text: str, conversation_id: str) -> str | None:
        from backend.services.sandbox import workspace_for
        from backend.utils.fs import atomic_write_text

        try:
            workspace = workspace_for(self.settings.workspaces_dir, conversation_id)
            atomic_write_text(workspace.runtime_path("request.md"), text)
        except Exception:
            return None
        return ".toolloom/request.md"

    def render_task_block(
        self, conversation_id: str, *, max_chars: int = MAX_BLOCK_CHARS
    ) -> str:
        """The pinned objective, bounded as a *view* of the durable record.

        The record is never trimmed. This rendering is, because a schema-valid plan
        can hold 25,000 characters and the model must still be able to receive a
        prompt and revise it. Active and next steps are shown first, completed work
        is summarised, and the complete plan stays retrievable by ``plan_read``.
        """
        task = self.active(conversation_id)
        if task is None:
            return ""
        with self.db.session() as session:
            plan = TaskRepository(session).plan_of(task)

        # Split the view budget so the plan always retains a share; the old fixed
        # objective/request shares could consume a small model's whole allowance and
        # leave the plan to be chopped off by the final guard.
        share = max(120, max_chars // 4)
        criteria = (
            _bound(task.completion_criteria.strip(), share)
            if task.completion_criteria
            else ""
        )
        objective = _bound(task.objective.strip(), share)
        request = self._request_for_prompt(task, conversation_id, share)
        lines = ["<task>", f"status: {task.status}"]
        if request:
            lines.append(f"original_request: {request}")
        lines.append(f"objective (rev {task.objective_revision}): {objective}")
        if criteria:
            lines.append(f"completion_criteria: {criteria}")
        used = sum(len(line) + 1 for line in lines) + len("</task>")
        plan_budget = max(120, max_chars - used - 20)
        lines.extend(_render_plan(plan, task.plan_revision, plan_budget))
        return _bound("\n".join(lines + ["</task>"]), max_chars)

    def render_execution_block(
        self, conversation_id: str, ledger, *, max_chars: int = MAX_BLOCK_CHARS
    ) -> str:
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
        return "\n".join(lines)[:max_chars]

    def project_plan(self, workspace, conversation_id: str) -> None:
        """Write the runtime-owned plan projection (``.toolloom/plan.md``).

        Best-effort by design: a projection that cannot be written must never fail a
        run, because the authoritative copy is in SQLite either way. It no longer
        uses the ordinary ``plan.md`` filename, which belongs to the operator/model.
        """
        task = self.active(conversation_id)
        if task is None:
            # A projection is a view of the active task, not the previous task.
            workspace.resolve(".toolloom/plan.md").unlink(missing_ok=True)
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
            from backend.utils.fs import atomic_write_text

            atomic_write_text(workspace.runtime_path("plan.md"), "\n".join(body) + "\n")
        except OSError:
            return
