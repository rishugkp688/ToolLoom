"""Backend-owned agent reasoning loop."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from backend.models.run import CANCELLED, COMPLETED, FAILED, INTERRUPTED
from backend.repositories import (
    ConversationRepository,
    MessageRepository,
    RunRepository,
    SettingRepository,
)
from backend.runtime.events import (
    COMPACTED,
    DONE,
    ERROR,
    ITERATION_START,
    STEP_NOTE,
    TOKEN,
    TOOL_CALL_END,
    TOOL_CALL_START,
    TOOL_CALL_VALIDATED,
    USAGE,
    AgentEvent,
)
from backend.runtime.observation import ExecutionResult, to_result
from backend.runtime.step_controller import StepController
from backend.services.attempts import AttemptLedger, arguments_digest
from backend.services.context_builder import UsageAnchor, anchor_for
from backend.services.context_policy import ContextPolicy
from backend.services.providers.base import (
    BaseProvider,
    MessageStopEvent,
    TextBlock,
    TextDelta,
    ToolCallBlock,
    ToolCallDeltaEvent,
    ToolCallStartEvent,
    ToolResultBlock,
    Usage,
    UsageEvent,
    blocks_to_dicts,
)
from backend.services.task_service import TaskService

logger = logging.getLogger(__name__)


class AgentRuntime:
    def __init__(
        self,
        db,
        settings,
        provider: BaseProvider,
        tool_executor=None,
        compactor=None,
        model_catalogue=None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.provider = provider
        self.tool_executor = tool_executor
        self.compactor = compactor
        self.model_catalogue = model_catalogue
        # Durable state lives beside the loop: the task is what the run is *for*, the
        # ledger is what actually happened, and the policy is what fits the prompt.
        self.tasks = TaskService(db, settings)
        self.ledger = AttemptLedger(db)

    async def run_stream(
        self, conversation_id: str, *, model_id: str | None = None
    ) -> AsyncIterator[AgentEvent]:
        settings = self.settings
        model = model_id or self._default_model_id()
        max_iterations = max(1, settings.toolloom_max_iterations)
        max_tool_calls = max(1, settings.toolloom_max_tool_calls)
        # Configurable, not a constant: how many times a truncated turn may be
        # continued is a deployment choice, like every other budget here.
        max_continuations = max(0, settings.toolloom_max_output_continuations)
        with self.db.session() as session:
            unlimited = SettingRepository(session).get().unlimited_tools

        controller = StepController()
        total_usage = Usage()
        tool_trace: list[dict] = []
        reply = ""
        partial_text = ""
        finish_reason = None
        stop_reason = "end_turn"
        iteration = 0
        tool_calls_made = 0
        # A turn cut off by the output limit is continued automatically; these track
        # the accumulated text and how many continuations have been spent.
        continuation_text = ""
        continuations = 0
        advisory: str | None = None
        with self.db.session() as session:
            repo = MessageRepository(session)
            seen_call_ids = {
                block.get("id")
                for row in repo.list_by_conversation(conversation_id)
                for block in repo.blocks(row)
                if block.get("type") == "tool_use"
            }

        # The task is what this run is for; the run is one attempt at it. Both are
        # durable, so a stop of any kind leaves a record a resume can read.
        task = self.tasks.ensure(conversation_id, request=self._first_request(conversation_id))
        with self.db.session() as session:
            run_id = RunRepository(session).start(conversation_id, task_id=task.id).id
        outcome = {"status": INTERRUPTED, "stop_reason": None, "error": "the run did not finish"}

        try:
            from backend.services.model_catalogue import budget_for

            budget = (
                await self.model_catalogue.budget(model)
                if self.model_catalogue
                else budget_for(settings)
            )
            policy = ContextPolicy(self.db, settings, self.compactor)
            anchor: UsageAnchor | None = None

            advisory = self._resume_advisory(conversation_id)

            while True:
                iteration += 1
                # Re-render the pinned state every iteration: plan_write can revise the
                # objective or plan mid-run, and the next provider request must see it.
                # A changed block also changes the system digest, which invalidates the
                # token anchor automatically.
                pinned = self._pinned_state(conversation_id)
                if not unlimited and iteration > max_iterations:
                    finish_reason = finish_reason or "max_iterations"
                if finish_reason:
                    advisory = (
                        "Tool execution has stopped (" + finish_reason + "). "
                        "Give the user your best answer from existing results, state what remains "
                        "unfinished, and do not request more tools."
                    )
                built, reduction = await policy.prepare(
                    conversation_id,
                    advisory=advisory,
                    pinned=pinned,
                    anchor=anchor,
                    model_id=model,
                    budget=budget,
                )
                if reduction:
                    # Emitted only when history was actually reduced, so the indicator
                    # means something rather than announcing a routine check.
                    yield AgentEvent(COMPACTED, {"reduction": reduction})
                advisory = None
                yield AgentEvent(ITERATION_START, {"iteration": iteration})

                turn = None
                async for event in self.provider.stream(
                    messages=built.messages,
                    system=built.system,
                    tools=[] if finish_reason else built.tools,
                    model_id=model,
                    max_tokens=budget.max_output_tokens,
                ):
                    if isinstance(event, TextDelta):
                        partial_text += event.text
                        yield AgentEvent(TOKEN, {"text": event.text})
                    elif isinstance(event, ToolCallStartEvent):
                        yield AgentEvent(
                            TOOL_CALL_START,
                            {"tool_call_id": event.id, "name": event.name},
                        )
                    elif isinstance(event, ToolCallDeltaEvent):
                        continue
                    elif isinstance(event, UsageEvent):
                        yield AgentEvent(USAGE, event.usage.to_dict())
                    elif isinstance(event, MessageStopEvent):
                        turn = event.turn

                if turn is None:
                    outcome = {
                        "status": FAILED,
                        "stop_reason": "error",
                        "error": "provider returned no result",
                    }
                    self._persist_note(conversation_id, "[provider returned no result]", "error")
                    yield AgentEvent(ERROR, {"message": "provider returned no result"})
                    return

                call_ids = [call.id for call in turn.tool_calls]
                if len(set(call_ids)) != len(call_ids) or any(
                    not cid or cid in seen_call_ids for cid in call_ids
                ):
                    outcome = {
                        "status": FAILED,
                        "stop_reason": "error",
                        "error": "provider reused a tool-call ID",
                    }
                    self._persist_note(
                        conversation_id, "[stopped: duplicate tool-call ID]", "error"
                    )
                    yield AgentEvent(
                        ERROR,
                        {"message": "provider reused a tool-call ID; no duplicate call executed"},
                    )
                    return
                seen_call_ids.update(call_ids)
                if finish_reason and not turn.tool_calls:
                    turn.stop_reason = finish_reason
                self._persist_assistant(conversation_id, turn)
                partial_text = ""
                total_usage.input_tokens += turn.usage.input_tokens
                total_usage.output_tokens += turn.usage.output_tokens
                if turn.usage.input_tokens:
                    # The provider's own count for the prompt we just sent is a far
                    # better basis than a character estimate for deciding what to prune.
                    anchor = anchor_for(
                        built.messages, built.system, built.tools, turn.usage.input_tokens
                    )
                stop_reason = turn.stop_reason
                if turn.text:
                    controller.note_text()
                    if stop_reason == "max_tokens":
                        # Hold this slice until the continuation finishes; updating the
                        # reply here would make each slice overwrite the previous one.
                        continuation_text += turn.text
                    else:
                        reply = (
                            continuation_text + turn.text if continuation_text else turn.text
                        )
                        continuation_text = ""
                if stop_reason == "error":
                    outcome = {
                        "status": FAILED,
                        "stop_reason": "error",
                        "error": "provider stream ended without a complete response",
                    }
                    yield AgentEvent(
                        ERROR,
                        {
                            "message": (
                                "Provider stream ended without a complete response. "
                                "You can continue."
                            )
                        },
                    )
                    return

                if finish_reason:
                    # A provider may ignore the empty tool catalogue. Never execute those calls.
                    for call in turn.tool_calls:
                        result = to_result(
                            call,
                            "Tool execution stopped; this call was not executed.",
                            is_error=True,
                        )
                        self._persist_tool_result(conversation_id, result)
                        tool_trace.append(_trace(result))
                        yield AgentEvent(TOOL_CALL_END, _end_payload(result, iteration))
                    stop_reason = finish_reason
                    if turn.tool_calls:
                        self._persist_note(
                            conversation_id, "[stopped: tool execution limit]", stop_reason
                        )
                    break
                if stop_reason == "max_tokens":
                    # The provider cut the turn off mid-output. A truncated answer is not
                    # a finished one, so the run continues instead of ending and making
                    # the operator click Continue after every file. Any tool call in such
                    # a turn is refused: its arguments may be truncated while still
                    # parsing as JSON, which would write a partial file.
                    for call in turn.tool_calls:
                        result = to_result(
                            call,
                            "Your previous turn was cut off by the output limit, so this "
                            "call was not executed. Re-issue it with smaller content, for "
                            "example by writing a file in parts.",
                            is_error=True,
                        )
                        self._persist_tool_result(conversation_id, result)
                        tool_trace.append(_trace(result))
                        yield AgentEvent(TOOL_CALL_END, _end_payload(result, iteration))

                    # The slice was accumulated above with the rest of the text.
                    if not turn.text and not turn.tool_calls:
                        # A budget consumed entirely (typically by reasoning) produces
                        # nothing to continue; spending three more turns would repeat
                        # the same thing. Stop and say what to change.
                        finish_reason = "max_tokens"
                        if not reply and not continuation_text:
                            reply = (
                                "The model reached its output limit before producing an "
                                "answer. Raise MODEL_MAX_OUTPUT_TOKENS in .env, or choose a "
                                "model that spends less of that budget on reasoning."
                            )
                        self._persist_note(
                            conversation_id,
                            "[stopped: output limit reached before any content]",
                            "max_tokens",
                        )
                        break
                    continuations += 1
                    if continuations > max_continuations:
                        reply = continuation_text or reply
                        finish_reason = "max_tokens"
                        self._persist_note(
                            conversation_id,
                            "[stopped: output limit reached repeatedly]",
                            "max_tokens",
                        )
                        break
                    advisory = (
                        "Your previous message was cut off by the output limit. Continue "
                        "exactly where you stopped, and keep each tool call small enough to "
                        "fit — write large files in parts rather than all at once."
                    )
                    continue

                # max_tokens was handled above; anything else that is not a complete
                # tool-use turn is the end of the run.
                if turn.stop_reason != "tool_use" or not turn.tool_calls:
                    break

                for call in turn.tool_calls:
                    exhausted = not unlimited and tool_calls_made >= max_tool_calls
                    tool_calls_made += 1

                    for note in controller.note_call(call.name, call.arguments or {}):
                        yield AgentEvent(STEP_NOTE, _note_payload(note))

                    refused = False
                    if exhausted or finish_reason:
                        finish_reason = finish_reason or "max_iterations"
                        refused = True
                        result = to_result(
                            call,
                            "Tool execution stopped; this call was not executed.",
                            is_error=True,
                        )
                    elif controller.should_refuse(call.name, call.arguments or {}):
                        refused = True
                        # Refuse this call, not the run. A repeated read is often a
                        # symptom of context reduction rather than a loop, and ending
                        # a productive run over it would discard the work. The
                        # iteration and tool-call budgets already bound the run.
                        result = to_result(
                            call,
                            f"refused: `{call.name}` was called with identical arguments "
                            "repeatedly, or kept failing. Try a different approach, or "
                            "answer with what you already have.",
                            is_error=True,
                        )
                    else:
                        result = await self._execute(call, conversation_id, run_id)

                    if result.validation_errors:
                        yield AgentEvent(
                            TOOL_CALL_VALIDATED,
                            {
                                "tool_call_id": result.tool_call_id,
                                "name": result.name,
                                "errors": result.validation_errors,
                            },
                        )

                    self._persist_tool_result(conversation_id, result)
                    tool_trace.append(_trace(result))
                    yield AgentEvent(TOOL_CALL_END, _end_payload(result, iteration))

                    # A successful mutating call is progress: repeating an earlier call
                    # (same test command after an edit) is no longer a duplicate.
                    changed = (
                        not result.is_error
                        and self.tool_executor is not None
                        and self.tool_executor.effect_of(call.name) == "mutating"
                    )
                    note = controller.note_result(
                        call.name,
                        result.is_error,
                        arguments=call.arguments or {},
                        changed=changed,
                        refused=refused,
                    )
                    if note is not None:
                        yield AgentEvent(STEP_NOTE, _note_payload(note))

                advisory = controller.advice()
                if not unlimited and tool_calls_made >= max_tool_calls:
                    finish_reason = finish_reason or "max_iterations"

            self._touch(conversation_id)
            outcome = {"status": COMPLETED, "stop_reason": stop_reason, "error": None}
            yield AgentEvent(
                DONE,
                {
                    "reply": reply,
                    "iterations": iteration,
                    "stop_reason": stop_reason,
                    "usage": total_usage.to_dict(),
                    "tool_calls": tool_trace,
                },
            )
        except GeneratorExit:
            # Closing the generator (client disconnect, loop shutdown) takes neither of
            # the branches below, so the partial answer and a stop marker were lost.
            outcome = {"status": INTERRUPTED, "stop_reason": "cancelled", "error": None}
            self._record_failure(conversation_id, partial_text, "cancelled")
            raise
        except asyncio.CancelledError:
            outcome = {"status": CANCELLED, "stop_reason": "cancelled", "error": None}
            self._record_failure(conversation_id, partial_text, "cancelled")
            raise
        except Exception as exc:  # never leak a raw traceback to the stream
            message = f"{type(exc).__name__}: {exc}"
            if self.tool_executor is not None:
                message = self.tool_executor._redact(message)
            outcome = {"status": FAILED, "stop_reason": "error", "error": type(exc).__name__}
            self._record_failure(conversation_id, partial_text, "error")
            yield AgentEvent(ERROR, {"message": message})
        finally:
            # Whatever happened — done, error, cancellation or the client vanishing —
            # the attempt is closed durably. This is what a resume reads.
            self._finish_run(run_id, outcome, iteration, tool_calls_made, conversation_id)

    def _record_failure(self, conversation_id: str, partial_text: str, reason: str) -> None:
        # Saving the failure must never mask cancellation or the original exception.
        try:
            if partial_text:
                if self.tool_executor is not None:
                    partial_text = self.tool_executor._redact(partial_text)
                self._persist_note(conversation_id, partial_text, reason)
            marker = "[run interrupted]" if reason == "cancelled" else "[run failed]"
            self._persist_note(conversation_id, marker, reason)
        except Exception as exc:
            logger.error("Could not persist interrupted run (%s)", type(exc).__name__)

    # -- helpers --------------------------------------------------------------

    def _default_model_id(self) -> str:
        model_id = getattr(self.provider, "model_id", None)
        if isinstance(model_id, str) and model_id:
            return model_id
        return self.settings.commandcode_model

    async def _execute(
        self, call: ToolCallBlock, conversation_id: str, run_id: str | None = None
    ) -> ExecutionResult:
        if self.tool_executor is None:
            return to_result(call, "Tools are not available in this build.", is_error=True)

        parse_error = getattr(call, "parse_error", None)
        if parse_error:
            # A malformed argument string is the model's mistake to correct, not a
            # reason to end a run that may already hold a lot of completed work.
            return to_result(
                call,
                f"(invalid arguments) The arguments for `{call.name}` could not be "
                f"parsed: {parse_error}. Re-issue the same call with one valid JSON "
                "object.",
                is_error=True,
            )

        digest = arguments_digest(call.arguments or {})
        effect = self.tool_executor.effect_of(call.name)

        # An earlier identical call whose outcome is unknown must not be repeated
        # blind: the side effect may already have landed. Checked before the ledger is
        # written, so the ledger only ever records dispatches that were attempted.
        if run_id is not None and effect != "read_only":
            prior = self.ledger.uncertain_match(conversation_id, call.name, digest)
            if prior is not None:
                return to_result(
                    call,
                    f"(refused) An earlier call to `{call.name}` with identical arguments "
                    f"has an unknown outcome ({prior['status']}): the process stopped before "
                    "its result was recorded, so the effect may or may not have happened. "
                    "Check the current state with a read-only tool first, then decide — do "
                    "not repeat it blindly.",
                    is_error=True,
                )

        attempt_id = None
        if run_id is not None:
            # Written before dispatch, so a crash leaves evidence that something was
            # attempted rather than a silent gap.
            attempt_id = self.ledger.begin(
                run_id,
                tool_call_id=call.id,
                namespaced_name=call.name,
                arguments_digest=digest,
                effect=effect,
            )
            self.ledger.dispatched(attempt_id)

        result = await self.tool_executor.execute(call, conversation_id)

        if attempt_id is not None:
            # Recorded independently of the SSE stream, so the outcome survives a
            # client that vanished mid-flight.
            self.ledger.settle(
                attempt_id,
                is_error=result.is_error,
                output_text=result.model_text,
                uncertain=result.outcome_uncertain,
            )
        return result

    def _first_request(self, conversation_id: str) -> str:
        """The user's opening request, for the task's immutable original_request."""
        with self.db.session() as session:
            for row in MessageRepository(session).list_by_conversation(conversation_id):
                text = (row.user_text or row.content or "").strip()
                if row.role == "user" and text:
                    return text[:2000]
        return ""

    def _pinned_state(self, conversation_id: str) -> str:
        """Task objective, plan and execution state, for the system prompt."""
        blocks = [
            self.tasks.render_task_block(conversation_id),
            self.tasks.render_execution_block(conversation_id, self.ledger),
        ]
        return "\n\n".join(block for block in blocks if block)

    def _finish_run(
        self, run_id: str, outcome: dict, iterations: int, tool_calls: int, conversation_id: str
    ) -> None:
        """Close the run record. Never fatal: bookkeeping must not mask the run."""
        try:
            with self.db.session() as session:
                repository = RunRepository(session)
                run = repository.get(run_id)
                if run is not None:
                    repository.finish(
                        run,
                        status=outcome["status"],
                        stop_reason=outcome["stop_reason"],
                        error=outcome["error"],
                        iterations=iterations,
                        tool_calls=tool_calls,
                    )
            from backend.services.sandbox.workspace import workspace_for

            self.tasks.project_plan(
                workspace_for(self.settings.workspaces_dir, conversation_id), conversation_id
            )
        except Exception as exc:
            logger.warning("Could not close the run record (%s: %s)", type(exc).__name__, exc)

    def _resume_advisory(self, conversation_id: str) -> str | None:
        """Deterministic starting context when the user resumes a stopped turn."""
        from backend.services.sandbox.workspace import workspace_for

        with self.db.session() as session:
            rows = MessageRepository(session).list_by_conversation(conversation_id)
        if not rows or rows[-1].role != "user" or not rows[-1].is_resume:
            return None

        recent_tools = [row.tool_name for row in rows if row.role == "tool"][-5:]
        lines = ["<resume_context>", "The previous turn was stopped by a budget or truncation."]

        # What the last attempt actually did, so the model resumes from evidence
        # instead of being told to carry on and guessing what already happened.
        with self.db.session() as session:
            previous = RunRepository(session).last_settled(conversation_id)
        if previous is not None:
            detail = previous.stop_reason or previous.error or "stopped"
            lines.append(
                f"Previous run: {previous.status} after {previous.iterations} "
                f"iteration(s) ({detail})."
            )
        evidence = self.ledger.describe_conversation(conversation_id)
        if evidence["completed"]:
            lines.append(
                "Already completed, do not repeat: "
                + ", ".join(item["tool"] for item in evidence["completed"])
            )
        if evidence["uncertain"]:
            lines.append(
                "Outcomes that are unknown and must be checked before repeating: "
                + ", ".join(f"{item['tool']} ({item['status']})" for item in evidence["uncertain"])
            )

        if recent_tools:
            lines.append("Recent tool calls: " + ", ".join(str(name) for name in recent_tools))
        try:
            workspace = workspace_for(self.settings.workspaces_dir, conversation_id)
            files = [
                workspace.relative(path)
                for path in sorted(workspace.root.rglob("*"))
                if path.is_file() and not workspace.is_hidden(path)
            ][:20]
        except Exception:
            files = []
        if files:
            lines.append("Workspace files: " + ", ".join(files))
        lines.append("Continue from where you stopped.")
        lines.append("</resume_context>")
        return "\n".join(lines)

    def _persist_assistant(self, conversation_id: str, turn) -> None:
        with self.db.session() as session:
            MessageRepository(session).add(
                conversation_id=conversation_id,
                role="assistant",
                content=turn.text,
                blocks=blocks_to_dicts(turn.blocks),
                usage_input=turn.usage.input_tokens,
                usage_output=turn.usage.output_tokens,
                stop_reason=turn.stop_reason,
            )
            repo = ConversationRepository(session)
            conversation = repo.get(conversation_id)
            if conversation is not None:
                repo.touch(conversation)

    def _persist_tool_result(self, conversation_id: str, result: ExecutionResult) -> None:
        block = ToolResultBlock(
            tool_call_id=result.tool_call_id,
            content=result.model_text,
            is_error=result.is_error,
            name=result.name,
        )
        with self.db.session() as session:
            MessageRepository(session).add(
                conversation_id=conversation_id,
                role="tool",
                content=result.model_text,
                blocks=blocks_to_dicts([block]),
                tool_name=result.name,
                tool_call_id=result.tool_call_id,
            )

    def _persist_note(
        self, conversation_id: str, text: str, stop_reason: str | None = None
    ) -> None:
        with self.db.session() as session:
            MessageRepository(session).add(
                conversation_id=conversation_id,
                role="assistant",
                content=text,
                blocks=blocks_to_dicts([TextBlock(text=text)]),
                stop_reason=stop_reason,
            )

    def _touch(self, conversation_id: str) -> None:
        with self.db.session() as session:
            repo = ConversationRepository(session)
            conversation = repo.get(conversation_id)
            if conversation is not None:
                repo.touch(conversation)


def _note_payload(note) -> dict:
    return {"level": note.level, "message": note.message, "context": note.context}


def _trace(result: ExecutionResult) -> dict:
    return {
        "tool_call_id": result.tool_call_id,
        "name": result.name,
        "input": result.input,
        "is_error": result.is_error,
        "output_text": result.output_text,
        "attempts": result.attempts,
        "duration_ms": result.duration_ms,
    }


def _end_payload(result: ExecutionResult, iteration: int) -> dict:
    payload = _trace(result)
    payload["iteration"] = iteration
    return payload
