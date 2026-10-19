from __future__ import annotations

import asyncio
import heapq
import io
import json
import logging
import zipfile
from contextlib import aclosing
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import ConfigDict, Field

from backend.runtime.events import DONE, AgentEvent
from backend.schemas import (
    ConversationDetail,
    ConversationSummary,
    CreateConversationRequest,
    SendMessageRequest,
)
from backend.schemas.responses import SendMessageResponse, ToolCallTrace
from backend.schemas.safe import SafeModel
from backend.services import uploads as upload_service
from backend.services.conversation_service import ConversationService
from backend.services.sandbox.workspace import workspace_for
from backend.services.uploads import UploadError
from backend.utils.async_work import run_blocking

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/conversations", tags=["conversations"])

# Workspace archives are built in memory, so the total is bounded rather than
# streamed from disk. A larger workspace can still be downloaded file by file.
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

CONTINUE_PROMPT = "Continue from where you stopped."


def _service(request: Request) -> ConversationService:
    return ConversationService(request.app.state.db, request.app.state.settings)


def _workspace(request: Request, conversation_id: str):
    return workspace_for(request.app.state.settings.workspaces_dir, conversation_id)


# Strong references to background metadata tasks, so they are not collected before
# they finish.
_TITLE_TASKS: set[asyncio.Task] = set()


def _schedule_title(request: Request, conversation_id: str, model_id: str | None) -> None:
    """Run title generation after the response, without holding the run lock."""

    async def run() -> None:
        try:
            await request.app.state.conversation_titles.generate(conversation_id, model_id)
        except Exception as exc:
            # Metadata/storage errors here must not turn a saved reply into a failure.
            logger.warning("Title update failed (%s)", type(exc).__name__)

    task = asyncio.create_task(run())
    _TITLE_TASKS.add(task)
    task.add_done_callback(_TITLE_TASKS.discard)


def _sse(event: AgentEvent) -> str:
    payload = json.dumps(event.data, default=str)
    return f"event: {event.kind}\ndata: {payload}\n\n"


def _with_attachments(
    request: Request, conversation_id: str, message: str, paths: list[str]
) -> tuple[str, list[dict]]:
    if not paths:
        return message, []
    workspace = _workspace(request, conversation_id)
    saved = []
    for path in paths:
        try:
            saved.append(upload_service.load_saved_upload(workspace, path))
        except UploadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    block = upload_service.build_attachment_preview(saved)
    return (f"{block}\n\n{message}" if message else block), upload_service.attachment_metadata(
        saved
    )


def _result(final: dict) -> SendMessageResponse:
    return SendMessageResponse(
        reply=final["reply"],
        iterations=final["iterations"],
        stop_reason=final["stop_reason"],
        usage=final["usage"],
        tool_calls=[ToolCallTrace(**t) for t in final["tool_calls"]],
    )


async def _start(
    request, conversation_id, text, *, is_resume=False, user_text=None, attachments=None
):
    active = request.app.state.active_runs
    if conversation_id in active:
        raise HTTPException(status_code=409, detail="conversation already has an active run")
    if len(active) >= 16:
        raise HTTPException(status_code=429, detail="too many active runs; try again shortly")
    # Reserve before yielding to storage so two requests cannot both be admitted.
    active.add(conversation_id)
    try:
        saved = await run_blocking(
            _service(request).add_user_message,
            conversation_id,
            text,
            is_resume=is_resume,
            user_text=user_text,
            attachments=attachments,
        )
        if saved is None:
            raise HTTPException(status_code=404, detail="conversation not found")
    except BaseException:
        active.discard(conversation_id)
        raise


async def _events(request, conversation_id, model_id, *, report_start=False):
    finished = None
    # Name the conversation now, from the first message, rather than after the run.
    # A run that takes minutes used to leave the provisional title on screen the whole
    # time; the title service ignores its own repeat calls via a cooldown.
    _schedule_title(request, conversation_id, model_id)
    try:
        async with asyncio.timeout(request.app.state.settings.toolloom_run_timeout_seconds):
            options = {"report_start": True} if report_start else {}
            async with aclosing(request.app.state.runtime.run_stream(
                conversation_id, model_id=model_id, **options
            )) as source:
                async for event in source:
                    if event.kind == DONE:
                        finished = event
                    else:
                        if event.kind == "error":
                            event = AgentEvent("error", {
                                "status": 502, "category": "provider_protocol", **event.data,
                            })
                        yield event
    except TimeoutError:
        yield AgentEvent(
            "error",
            {
                "message": "Run time limit reached. You can continue the conversation.",
                "status": 504,
                "category": "run_timeout",
            },
        )
    except Exception as exc:
        logger.error("Run boundary failed (%s)", type(exc).__name__)
        yield AgentEvent(
            "error",
            {"message": "Run failed unexpectedly. Retry or check backend storage.",
             "status": 500, "category": "internal"},
        )
    if finished is not None:
        # The client gets its terminal event first. Title generation is metadata and
        # can be slow; holding the completion for it delayed the finished turn even
        # though the reply was already saved.
        yield finished
        # Any finished run may deserve a name, including one that stopped on a budget
        # or at the output limit. The title service owns its own guards (attempts,
        # cooldown, provisional titles), so it decides whether to call the model.
        _schedule_title(request, conversation_id, model_id)


async def _drain(request, conversation_id, model_id):
    final = None
    failure = {}
    try:
        async for event in _events(request, conversation_id, model_id):
            if event.kind == DONE:
                final = event.data
            elif event.kind == "error":
                failure = event.data
    finally:
        request.app.state.active_runs.discard(conversation_id)
    if final is None:
        raise HTTPException(
            status_code=failure.get("status", 502),
            detail=failure.get("message", "agent produced no result"),
            headers={"X-ToolLoom-Error": failure.get("category", "provider")},
        )
    return final


class _RunResponse(StreamingResponse):
    def __init__(self, request, conversation_id, model_id):
        self.active = request.app.state.active_runs
        self.conversation_id = conversation_id

        async def source():
            async with aclosing(_events(request, conversation_id, model_id)) as events:
                async for event in events:
                    yield _sse(event)

        super().__init__(source(), media_type="text/event-stream", headers=SSE_HEADERS)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.active.discard(self.conversation_id)


# --- conversations -----------------------------------------------------------


@router.post("", response_model=ConversationSummary, status_code=201)
def create_conversation(body: CreateConversationRequest, request: Request) -> dict:
    return _service(request).create(body.title)


@router.get("", response_model=list[ConversationSummary])
def list_conversations(request: Request) -> list[dict]:
    return _service(request).list()


@router.get("/{conversation_id}/meta", response_model=ConversationSummary)
def get_conversation_meta(conversation_id: str, request: Request) -> dict:
    """Header metadata only, for cheap title polling while a run streams."""
    meta = _service(request).meta(conversation_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return meta


@router.get("/{conversation_id}", response_model=ConversationDetail)
def get_conversation(
    conversation_id: str, request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    before: str | None = Query(default=None, min_length=1, max_length=36),
) -> dict:
    try:
        detail = _service(request).get(conversation_id, limit=limit, before=before)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if detail is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return detail


@router.delete("/{conversation_id}", status_code=204)
async def delete_conversation(conversation_id: str, request: Request) -> None:
    if conversation_id in request.app.state.active_runs:
        raise HTTPException(status_code=409, detail="stop the active run before deleting")
    if not _service(request).delete(conversation_id):
        raise HTTPException(status_code=404, detail="conversation not found")
    workspace = _workspace(request, conversation_id)
    try:
        import shutil

        if workspace.root.exists():
            shutil.rmtree(workspace.root, ignore_errors=True)
    except Exception:
        pass  # best-effort cleanup; a stale lock must not fail the delete


# --- turns -------------------------------------------------------------------


@router.post("/{conversation_id}/messages", response_model=SendMessageResponse)
async def send_message(
    conversation_id: str, body: SendMessageRequest, request: Request
) -> SendMessageResponse:
    if await run_blocking(_service(request).meta, conversation_id) is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    text, attachments = await run_blocking(
        _with_attachments, request, conversation_id, body.message, body.attachments
    )
    await _start(request, conversation_id, text, user_text=body.message, attachments=attachments)
    return _result(await _drain(request, conversation_id, body.model_id))


@router.post("/{conversation_id}/messages/stream")
async def stream_message(conversation_id: str, body: SendMessageRequest, request: Request):
    if await run_blocking(_service(request).meta, conversation_id) is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    text, attachments = await run_blocking(
        _with_attachments, request, conversation_id, body.message, body.attachments
    )
    await _start(request, conversation_id, text, user_text=body.message, attachments=attachments)
    return _RunResponse(request, conversation_id, body.model_id)


@router.post("/{conversation_id}/continue", response_model=SendMessageResponse)
async def continue_turn(conversation_id: str, request: Request, model_id: str | None = None):
    await _start(request, conversation_id, CONTINUE_PROMPT, is_resume=True)
    return _result(await _drain(request, conversation_id, model_id))


@router.post("/{conversation_id}/continue/stream")
async def continue_stream(conversation_id: str, request: Request, model_id: str | None = None):
    await _start(request, conversation_id, CONTINUE_PROMPT, is_resume=True)
    return _RunResponse(request, conversation_id, model_id)


# --- supervised runs: disconnecting a reader never cancels execution ----------

@router.post("/{conversation_id}/runs", status_code=202)
async def start_supervised_run(
    conversation_id: str, request: Request, body: SendMessageRequest | None = None,
    resume: bool = False, model_id: str | None = None,
):
    from backend.services.run_supervisor import RunStartFailed

    if not resume and body is None:
        raise HTTPException(status_code=422, detail="message body required")
    if await run_blocking(_service(request).meta, conversation_id) is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    if resume:
        await _start(request, conversation_id, CONTINUE_PROMPT, is_resume=True)
    else:
        text, attachments = await run_blocking(
            _with_attachments, request, conversation_id, body.message, body.attachments
        )
        await _start(
            request, conversation_id, text, user_text=body.message, attachments=attachments
        )
        model_id = body.model_id or model_id
    try:
        run_id = await request.app.state.run_supervisor.start(
            conversation_id, _events(request, conversation_id, model_id, report_start=True)
        )
    except RunStartFailed as exc:
        raise HTTPException(
            status_code=exc.payload["status"], detail=exc.payload["message"],
            headers={"X-ToolLoom-Error": exc.payload["category"]},
        ) from exc
    return {
        "run_id": run_id,
        "events_url": f"/conversations/{conversation_id}/runs/{run_id}/events",
    }


@router.get("/{conversation_id}/runs/active")
async def active_supervised_run(conversation_id: str, request: Request):
    if await run_blocking(_service(request).meta, conversation_id) is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    handle = request.app.state.run_supervisor.live(conversation_id)
    return {"run_id": handle.run_id if handle else None}


async def _supervised_info(request, conversation_id, run_id):
    info = await run_blocking(request.app.state.run_supervisor.info, conversation_id, run_id)
    if info is None:
        raise HTTPException(status_code=404, detail="run not found")
    return info


@router.get("/{conversation_id}/runs/{run_id}")
async def supervised_run_info(conversation_id: str, run_id: str, request: Request):
    return await _supervised_info(request, conversation_id, run_id)


@router.get("/{conversation_id}/runs/{run_id}/events")
async def attach_run(
    conversation_id: str, run_id: str, request: Request,
    after: int = Query(default=0, ge=0, le=2**63 - 1),
):
    info = await _supervised_info(request, conversation_id, run_id)
    if not info["replay_available"]:
        raise HTTPException(status_code=409, detail="this run has no supervised event journal")
    header = request.headers.get("last-event-id")
    if header is not None:
        try:
            header_cursor = int(header)
            if header_cursor < 0:
                raise ValueError
            after = max(after, header_cursor)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid Last-Event-ID") from exc
    if after > info["last_event_sequence"]:
        raise HTTPException(status_code=422, detail="cursor is beyond the saved event journal")
    supervisor = request.app.state.run_supervisor

    async def source():
        cursor = after
        heartbeat = asyncio.get_running_loop().time()
        while True:
            rows = await run_blocking(supervisor.read, run_id, cursor)
            for sequence, event in rows:
                cursor = sequence
                yield f"id: {sequence}\n{_sse(event)}"
                if event.kind in {"done", "error"}:
                    return
            if rows:
                continue
            if not supervisor.live(conversation_id, run_id):
                # Final journal commits precede release of run ownership. Recheck
                # once after observing release to close the empty-read race.
                rows = await run_blocking(supervisor.read, run_id, cursor)
                if rows:
                    continue
                info = await _supervised_info(request, conversation_id, run_id)
                if info["status"] in {"interrupted", "running"}:
                    yield _sse(AgentEvent("error", {
                        "message": "Run interrupted; inspect saved state before Continue.",
                        "category": "run_interrupted", "status": 409,
                    }))
                elif info["last_event_kind"] not in {"done", "error"}:
                    yield _sse(AgentEvent("error", {
                        "message": "Final progress event was not saved. Check the transcript.",
                        "category": "event_storage", "status": 500,
                    }))
                return
            if asyncio.get_running_loop().time() - heartbeat >= 15:
                heartbeat = asyncio.get_running_loop().time()
                yield ": keep-alive\n\n"
            await asyncio.sleep(0.1)

    return StreamingResponse(source(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.post("/{conversation_id}/runs/{run_id}/cancel", status_code=202)
async def cancel_supervised_run(conversation_id: str, run_id: str, request: Request):
    await _supervised_info(request, conversation_id, run_id)
    if not request.app.state.run_supervisor.cancel(conversation_id, run_id):
        raise HTTPException(status_code=409, detail="run is not live in this backend")
    return {"run_id": run_id, "cancel_requested": True}


class SteeringInput(SafeModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=64_000, pattern=r"\S")


@router.post("/{conversation_id}/runs/{run_id}/steer", status_code=202)
async def steer_supervised_run(
    conversation_id: str, run_id: str, body: SteeringInput, request: Request,
):
    await _supervised_info(request, conversation_id, run_id)
    supervisor = request.app.state.run_supervisor
    if not supervisor.live(conversation_id, run_id):
        raise HTTPException(status_code=409, detail="run is not live in this backend")
    input_id = await run_blocking(supervisor.steer, conversation_id, run_id, body.message)
    return {"id": input_id, "queued": True,
            "delivery": "next model request, or Continue if the run has already stopped"}


# --- files & workspace -------------------------------------------------------


@router.get("/{conversation_id}/task")
def get_task(conversation_id: str, request: Request) -> dict:
    """The durable objective and plan, so the UI can show what the agent is doing."""
    if _service(request).meta(conversation_id) is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    runtime = request.app.state.runtime
    task = runtime.tasks.active(conversation_id)
    if task is None:
        return {"task": None}
    return {
        "task": {
            "id": task.id,
            "original_request": task.original_request,
            "objective": task.objective,
            "objective_revision": task.objective_revision,
            "plan": runtime.tasks.plan_of(task),
            "plan_revision": task.plan_revision,
            "completion_criteria": task.completion_criteria,
            "status": task.status,
        }
    }


@router.get("/{conversation_id}/runs")
def list_runs(conversation_id: str, request: Request) -> dict:
    """Execution attempts, most recent first, with their recorded outcomes."""
    if _service(request).meta(conversation_id) is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    from backend.repositories import RunRepository

    with request.app.state.db.session() as session:
        runs = RunRepository(session).list_for_conversation(conversation_id)
        return {
            "runs": [
                {
                    "id": run.id,
                    "status": run.status,
                    "stop_reason": run.stop_reason,
                    "error": run.error,
                    "iterations": run.iterations,
                    "tool_calls": run.tool_calls,
                    "started_at": run.started_at,
                    "ended_at": run.ended_at,
                }
                for run in runs
            ]
        }


class ReconcileRequest(SafeModel):
    note: str = Field(default="Operator checked the current state", min_length=1, max_length=2000)


@router.get("/{conversation_id}/attempts")
def list_uncertain_attempts(conversation_id: str, request: Request) -> dict:
    if _service(request).meta(conversation_id) is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    rows = request.app.state.runtime.ledger.uncertain_details(conversation_id)
    redact = request.app.state.tool_executor._redact_arguments
    for row in rows:
        row["arguments"] = redact(row["arguments"])
    return {"attempts": rows}


@router.post("/{conversation_id}/attempts/{attempt_id}/reconcile", status_code=204)
def reconcile_attempt(
    conversation_id: str,
    attempt_id: str,
    request: Request,
    body: ReconcileRequest | None = None,
) -> None:
    """Resolve an uncertain tool outcome after the operator has checked it.

    Deliberately human-driven. The runtime cannot know whether a side effect landed,
    and letting the model adjudicate its own uncertain effect would defeat the point.
    """
    if _service(request).meta(conversation_id) is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    ledger = request.app.state.runtime.ledger
    known = {item["id"] for item in ledger.describe_conversation(conversation_id)["uncertain"]}
    if attempt_id not in known:
        raise HTTPException(status_code=404, detail="attempt not found or already settled")
    if conversation_id in request.app.state.active_runs:
        raise HTTPException(status_code=409, detail="stop the active run before reconciling")
    if request.app.state.mcp_connection_manager.workspace_busy(
        _workspace(request, conversation_id).root
    ):
        raise HTTPException(
            status_code=409, detail="a local operation is still finishing; retry shortly"
        )
    ledger.mark_reconciled(attempt_id, (body or ReconcileRequest()).note)


@router.post("/{conversation_id}/files", status_code=201)
async def upload_files(
    conversation_id: str, request: Request, files: list[UploadFile] = File(...)
) -> dict:
    if _service(request).meta(conversation_id) is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    workspace = _workspace(request, conversation_id)
    saved = []
    if len(files) > 10:
        raise HTTPException(status_code=400, detail="at most 10 files per upload")
    for upload in files:
        raw = await upload.read(upload_service.MAX_UPLOAD_BYTES + 1)
        try:
            saved.append(upload_service.save_upload(workspace, upload.filename or "upload", raw))
        except UploadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "files": [
            {
                "name": item.name,
                "path": item.path,
                "size_bytes": item.size_bytes,
                "inlined": item.inlined,
            }
            for item in saved
        ]
    }


@router.get("/{conversation_id}/workspace")
def list_workspace(conversation_id: str, request: Request) -> dict:
    if _service(request).meta(conversation_id) is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    workspace = _workspace(request, conversation_id)
    # Keep the 500 newest without materialising every path or sorting the whole tree.
    entries: list[tuple[float, str, int]] = []
    for path in workspace.iter_files(for_export=True):
        try:
            stat = path.stat()
        except OSError:
            continue
        item = (stat.st_mtime, workspace.relative(path), stat.st_size)
        if len(entries) < 500:
            heapq.heappush(entries, item)
        elif item > entries[0]:
            heapq.heapreplace(entries, item)
    entries.sort(reverse=True)
    return {
        "files": [
            {
                "name": Path(relative).name,
                "path": relative,
                "size_bytes": size,
                "modified_at": modified,
            }
            for modified, relative, size in entries
        ]
    }


@router.get("/{conversation_id}/workspace/download")
def download_workspace_file(conversation_id: str, path: str, request: Request) -> FileResponse:
    if _service(request).meta(conversation_id) is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    workspace = _workspace(request, conversation_id)
    try:
        target = workspace.resolve(path, must_exist=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if workspace.is_hidden(target) or not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(target, filename=target.name)


@router.get("/{conversation_id}/workspace/download-all")
def download_workspace_archive(conversation_id: str, request: Request) -> StreamingResponse:
    """Zip the whole workspace, applying the same rules as the file listing."""
    if _service(request).meta(conversation_id) is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    workspace = _workspace(request, conversation_id)

    files: list[tuple[str, Path]] = []
    total = 0
    for path in workspace.iter_files(for_export=True):
        try:
            total += path.stat().st_size
        except OSError:
            continue
        if total > MAX_ARCHIVE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=(
                    "this workspace is larger than the "
                    f"{MAX_ARCHIVE_BYTES // (1024 * 1024)} MB archive limit; "
                    "download individual files instead"
                ),
            )
        files.append((workspace.relative(path), path))
    if not files:
        raise HTTPException(status_code=404, detail="workspace is empty")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, path in files:
            archive.write(path, arcname=name)
    buffer.seek(0)

    filename = f"workspace-{conversation_id[:8]}.zip"
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
