"""Single-process run ownership, independent of any attached HTTP stream.

SQLite is the replay buffer. Slow/disconnected readers hold no producer queues.
This deliberately does not restart model/tool work after a process crash.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

import anyio
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from backend.models import Run, RunEvent, RunInput
from backend.runtime.events import AgentEvent
from backend.utils.async_work import run_blocking
from backend.utils.sqlite_retry import retry_sqlite_transaction

logger = logging.getLogger(__name__)


class RunStartFailed(RuntimeError):
    def __init__(self, payload):
        super().__init__(payload["message"])
        self.payload = payload


@dataclass
class _OwnedRun:
    task: asyncio.Task | None = None
    run_id: str | None = None


class RunSupervisor:
    def __init__(self, db, active):
        self.db = db
        self.active = active
        self.owned: dict[str, _OwnedRun] = {}
        self.closing = False

    async def start(self, conversation_id, source):
        if self.closing:
            self.active.discard(conversation_id)
            raise RunStartFailed({
                "message": "Backend is shutting down.", "status": 503, "category": "shutdown",
            })
        ready = asyncio.get_running_loop().create_future()
        ready.add_done_callback(
            lambda future: future.exception() if not future.cancelled() else None
        )
        handle = _OwnedRun()
        self.owned[conversation_id] = handle

        async def pump():
            sequence = 0
            batch = []
            last_flush = asyncio.get_running_loop().time()
            failure = None
            terminal = False
            flush_lock = asyncio.Lock()
            owner = asyncio.current_task()
            journal_failed = False

            async def flush():
                nonlocal sequence, last_flush
                async with flush_lock:
                    if batch and handle.run_id:
                        count = len(batch)
                        await run_blocking(self._append, handle.run_id, sequence, list(batch))
                        sequence += count
                        # Tokens can arrive while this worker commits its snapshot.
                        del batch[:count]
                        last_flush = asyncio.get_running_loop().time()

            async def periodic_flush():
                nonlocal journal_failed
                try:
                    while True:
                        await asyncio.sleep(0.1)
                        await flush()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    journal_failed = True
                    owner.cancel()

            flusher = asyncio.create_task(periodic_flush())

            try:
                async for event in source:
                    if event.kind == "run_started":
                        handle.run_id = event.data["run_id"]
                    if not handle.run_id:
                        failure = event.data
                        continue
                    batch.append(event)
                    terminal = event.kind in {"done", "error"}
                    if (event.kind != "token" or len(batch) >= 128
                            or asyncio.get_running_loop().time() - last_flush >= 0.1):
                        await flush()
                    if handle.run_id and not ready.done():
                        ready.set_result(handle.run_id)
            except asyncio.CancelledError:
                if journal_failed:
                    failure = {"message": "Run event storage failed. Check the transcript.",
                               "status": 500, "category": "event_storage"}
                else:
                    failure = {"message": "Run cancelled by the operator.", "status": 499,
                               "category": "cancelled", "stop_reason": "cancelled"}
            except Exception as exc:
                logger.error("Supervised run failed (%s)", type(exc).__name__)
                failure = {"message": "Run event storage failed. Check the saved transcript.",
                           "status": 500, "category": "event_storage"}
            finally:
                with anyio.CancelScope(shield=True):
                    try:
                        flusher.cancel()
                        await asyncio.gather(flusher, return_exceptions=True)
                        await source.aclose()
                        if failure and not terminal:
                            batch.append(AgentEvent("error", failure))
                        await flush()
                    except Exception as exc:
                        logger.error("Run journal cleanup failed (%s)", type(exc).__name__)
                    finally:
                        self.active.discard(conversation_id)
                        self.owned.pop(conversation_id, None)
                        if not ready.done():
                            ready.set_exception(RunStartFailed(failure or {
                                "message": "Run did not start.", "status": 500,
                                "category": "internal",
                            }))

        handle.task = asyncio.create_task(pump())
        def settled(task):
            # Cancellation before the coroutine's first instruction bypasses its
            # finally block. Keep admission and readiness correct in that case.
            if self.owned.get(conversation_id) is handle:
                self.owned.pop(conversation_id, None)
                self.active.discard(conversation_id)
            if not ready.done():
                ready.set_exception(RunStartFailed({
                    "message": "Run stopped before startup.", "status": 503,
                    "category": "shutdown",
                }))
            if not task.cancelled():
                task.exception()
        handle.task.add_done_callback(settled)
        return await asyncio.shield(ready)

    @retry_sqlite_transaction
    def _append(self, run_id, sequence, events):
        with self.db.session() as session:
            # A cancellation can arrive after the worker committed but before the
            # awaiter advanced its sequence. Reflushing that same batch is safe.
            for index, event in enumerate(events):
                session.execute(insert(RunEvent).values(
                    run_id=run_id, sequence=sequence + index + 1,
                    kind=event.kind, data_json=json.dumps(event.data, default=str),
                ).on_conflict_do_nothing(index_elements=[RunEvent.run_id, RunEvent.sequence]))

    def info(self, conversation_id, run_id):
        with self.db.session() as session:
            run = session.get(Run, run_id)
            if run is None or run.conversation_id != conversation_id:
                return None
            inputs = list(session.scalars(select(RunInput).where(RunInput.run_id == run_id)
                                          .order_by(RunInput.created_at, RunInput.id)))
            last_event = session.execute(select(RunEvent.sequence, RunEvent.kind).where(
                RunEvent.run_id == run_id,
            ).order_by(RunEvent.sequence.desc()).limit(1)).first()
            return {"id": run.id, "status": run.status, "stop_reason": run.stop_reason,
                    "replay_available": last_event is not None,
                    "last_event_sequence": last_event[0] if last_event else 0,
                    "last_event_kind": last_event[1] if last_event else None,
                    "inputs": [{"id": row.id, "message": row.message,
                                "delivered": row.delivered_at is not None,
                                "superseded": row.superseded_at is not None} for row in inputs]}

    def read(self, run_id, after):
        with self.db.session() as session:
            rows = session.scalars(select(RunEvent).where(
                RunEvent.run_id == run_id, RunEvent.sequence > after,
            ).order_by(RunEvent.sequence).limit(256))
            return [(row.sequence, AgentEvent(row.kind, json.loads(row.data_json))) for row in rows]

    def live(self, conversation_id, run_id=None):
        handle = self.owned.get(conversation_id)
        return handle if handle and (run_id is None or handle.run_id == run_id) else None

    def cancel(self, conversation_id, run_id):
        handle = self.live(conversation_id, run_id)
        if handle is None or handle.task.done():
            return False
        if not handle.task.cancelling():
            handle.task.cancel()
        return True

    def steer(self, conversation_id, run_id, message):
        with self.db.session() as session:
            row = RunInput(conversation_id=conversation_id, run_id=run_id, message=message)
            session.add(row)
            session.flush()
            return row.id

    async def aclose(self):
        self.closing = True
        tasks = [handle.task for handle in self.owned.values() if handle.task is not None]
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=10)
            if pending:
                logger.error("%d supervised runs still cleaning up at shutdown", len(pending))
