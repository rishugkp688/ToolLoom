"""Backend-owned agent reasoning loop."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from backend.repositories import ConversationRepository, MessageRepository, SettingRepository
from backend.runtime.events import (
    COMPACTED,
    COMPACTING,
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
from backend.services.context_builder import ContextBuilder
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

    async def run_stream(
        self, conversation_id: str, *, model_id: str | None = None
    ) -> AsyncIterator[AgentEvent]:
        settings = self.settings
        model = model_id or self._default_model_id()
        max_iterations = max(1, settings.toolloom_max_iterations)
        max_tool_calls = max(1, settings.toolloom_max_tool_calls)
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
        advisory: str | None = None
        with self.db.session() as session:
            repo = MessageRepository(session)
            seen_call_ids = {
                block.get("id")
                for row in repo.list_by_conversation(conversation_id)
                for block in repo.blocks(row)
                if block.get("type") == "tool_use"
            }

        try:
            from backend.services.model_catalogue import budget_for

            budget = (
                await self.model_catalogue.budget(model)
                if self.model_catalogue
                else budget_for(settings)
            )
            if self.compactor is not None:
                yield AgentEvent(COMPACTING, {})
                try:
                    if await self.compactor.compact_if_needed(
                        conversation_id, model_id=model, budget=budget
                    ):
                        yield AgentEvent(COMPACTED, {})
                except Exception:
                    pass  # compaction is never fatal

            advisory = self._resume_advisory(conversation_id)

            while True:
                iteration += 1
                if not unlimited and iteration > max_iterations:
                    finish_reason = finish_reason or "max_iterations"
                if finish_reason:
                    advisory = (
                        "Tool execution has stopped (" + finish_reason + "). "
                        "Give the user your best answer from existing results, state what remains "
                        "unfinished, and do not request more tools."
                    )
                built = ContextBuilder(self.db, settings, budget).build(
                    conversation_id, advisory=advisory
                )
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
                    self._persist_note(conversation_id, "[provider returned no result]", "error")
                    yield AgentEvent(ERROR, {"message": "provider returned no result"})
                    return

                call_ids = [call.id for call in turn.tool_calls]
                if len(set(call_ids)) != len(call_ids) or any(
                    not cid or cid in seen_call_ids for cid in call_ids
                ):
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
                if turn.text:
                    reply = turn.text
                    controller.note_text()
                stop_reason = turn.stop_reason
                if stop_reason == "error":
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
                if turn.stop_reason != "tool_use" or not turn.tool_calls:
                    break

                for call in turn.tool_calls:
                    exhausted = not unlimited and tool_calls_made >= max_tool_calls
                    tool_calls_made += 1

                    for note in controller.note_call(call.name, call.arguments or {}):
                        yield AgentEvent(STEP_NOTE, _note_payload(note))

                    if exhausted or finish_reason:
                        finish_reason = finish_reason or "max_iterations"
                        result = to_result(
                            call,
                            "Tool execution stopped; this call was not executed.",
                            is_error=True,
                        )
                    elif controller.should_refuse(call.name, call.arguments or {}):
                        finish_reason = "loop_guard"
                        result = to_result(
                            call,
                            f"refused: `{call.name}` repeated identical calls "
                            "or failed repeatedly this turn",
                            is_error=True,
                        )
                    else:
                        result = await self._execute(call, conversation_id)

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

                    note = controller.note_result(call.name, result.is_error)
                    if note is not None:
                        yield AgentEvent(STEP_NOTE, _note_payload(note))

                advisory = controller.advice()
                if not unlimited and tool_calls_made >= max_tool_calls:
                    finish_reason = finish_reason or "max_iterations"

            self._touch(conversation_id)
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
        except asyncio.CancelledError:
            self._record_failure(conversation_id, partial_text, "cancelled")
            raise
        except Exception as exc:  # never leak a raw traceback to the stream
            message = f"{type(exc).__name__}: {exc}"
            if self.tool_executor is not None:
                message = self.tool_executor._redact(message)
            self._record_failure(conversation_id, partial_text, "error")
            yield AgentEvent(ERROR, {"message": message})

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

    async def _execute(self, call: ToolCallBlock, conversation_id: str) -> ExecutionResult:
        if self.tool_executor is None:
            return to_result(call, "Tools are not available in this build.", is_error=True)
        return await self.tool_executor.execute(call, conversation_id)

    def _resume_advisory(self, conversation_id: str) -> str | None:
        """Deterministic starting context when the user resumes a stopped turn."""
        from backend.services.sandbox.workspace import workspace_for

        with self.db.session() as session:
            rows = MessageRepository(session).list_by_conversation(conversation_id)
        if not rows or rows[-1].role != "user" or not rows[-1].is_resume:
            return None

        recent_tools = [row.tool_name for row in rows if row.role == "tool"][-5:]
        lines = ["<resume_context>", "The previous turn was stopped by a budget or truncation."]
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
