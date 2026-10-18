"""The tool-attempt ledger: what was dispatched, and what we actually know about it.

Two rules drive everything here:

* **Record before dispatch.** A side effect can land just before the process dies.
  If the record is written first, the worst case is a row saying "we are not sure",
  which is recoverable. If it is written afterwards, the worst case is a silent
  duplicate on the retry.
* **Never invent certainty.** A row left mid-flight becomes ``unknown``. It is not
  retried unless the tool is one we know to be read-only, and resolving it is an
  operator decision rather than a guess.
"""

from __future__ import annotations

import hashlib
import json

from backend.models.tool_attempt import FAILED, RECONCILED, RUNNING, SUCCEEDED
from backend.repositories import ToolAttemptRepository


def arguments_digest(arguments: dict | None) -> str:
    canonical = json.dumps(arguments or {}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def result_digest(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


class AttemptLedger:
    def __init__(self, db) -> None:
        self.db = db

    def begin(
        self,
        run_id: str,
        *,
        tool_call_id: str,
        namespaced_name: str,
        arguments_digest: str,
        effect: str,
    ) -> str:
        """Persist the intent to dispatch, before dispatching."""
        with self.db.session() as session:
            attempt = ToolAttemptRepository(session).add(
                run_id,
                tool_call_id=tool_call_id,
                namespaced_name=namespaced_name,
                arguments_digest=arguments_digest,
                effect=effect,
            )
            return attempt.id

    def dispatched(self, attempt_id: str) -> None:
        with self.db.session() as session:
            attempt = ToolAttemptRepository(session).get(attempt_id)
            if attempt is not None:
                ToolAttemptRepository(session).set_status(attempt, RUNNING)

    def settle(
        self, attempt_id: str, *, is_error: bool, output_text: str, uncertain: bool = False
    ) -> None:
        """Record the outcome independently of the SSE stream.

        ``uncertain`` marks a failure that happened after dispatch and cannot be
        proven ineffective: the attempt becomes ``unknown``, not ``failed``, so the
        retry guard refuses an identical repeat.
        """
        from backend.models.tool_attempt import UNKNOWN

        with self.db.session() as session:
            repository = ToolAttemptRepository(session)
            attempt = repository.get(attempt_id)
            if attempt is None:
                return
            repository.set_status(
                attempt,
                UNKNOWN if uncertain else (FAILED if is_error else SUCCEEDED),
                is_error=is_error,
                result_digest=result_digest(output_text),
            )

    def reconcile_orphans(self) -> int:
        with self.db.session() as session:
            return ToolAttemptRepository(session).reconcile_orphans()

    def uncertain_match(
        self, conversation_id: str, namespaced_name: str, digest: str
    ) -> dict | None:
        with self.db.session() as session:
            attempt = ToolAttemptRepository(session).find_uncertain(
                conversation_id, namespaced_name, digest
            )
            if attempt is None:
                return None
            return {
                "id": attempt.id,
                "tool": attempt.namespaced_name,
                "effect": attempt.effect,
                "status": attempt.status,
            }

    def mark_reconciled(self, attempt_id: str) -> bool:
        with self.db.session() as session:
            repository = ToolAttemptRepository(session)
            attempt = repository.get(attempt_id)
            if attempt is None:
                return False
            repository.set_status(attempt, RECONCILED)
            return True

    def describe_conversation(self, conversation_id: str) -> dict:
        """The evidence a resume needs: what finished, what is uncertain."""
        with self.db.session() as session:
            repository = ToolAttemptRepository(session)
            completed = repository.settled_mutating_for_conversation(conversation_id)
            uncertain = repository.uncertain_for_conversation(conversation_id)
            return {
                "completed": [
                    {"tool": a.namespaced_name, "effect": a.effect} for a in completed
                ],
                "uncertain": [
                    {"id": a.id, "tool": a.namespaced_name, "status": a.status}
                    for a in uncertain
                ],
            }
