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
        arguments: dict | None = None,
    ) -> str:
        """Persist the intent to dispatch, before dispatching."""
        with self.db.session() as session:
            attempt = ToolAttemptRepository(session).add(
                run_id,
                tool_call_id=tool_call_id,
                namespaced_name=namespaced_name,
                arguments_digest=arguments_digest,
                effect=effect,
                arguments_json=json.dumps(arguments or {}, ensure_ascii=True),
            )
            return attempt.id

    def dispatched(self, attempt_id: str) -> None:
        with self.db.session() as session:
            attempt = ToolAttemptRepository(session).get(attempt_id)
            if attempt is not None:
                ToolAttemptRepository(session).set_status(attempt, RUNNING)

    def settle(
        self,
        attempt_id: str,
        *,
        is_error: bool,
        output_text: str,
        uncertain: bool = False,
        verification_failed: bool = False,
        session=None,
    ) -> None:
        """Record the outcome independently of the SSE stream.

        ``uncertain`` marks a failure that happened after dispatch and cannot be
        proven ineffective: the attempt becomes ``unknown``, not ``failed``, so the
        retry guard refuses an identical repeat.

        Passing ``session`` lets the caller settle and write the transcript entry in
        one transaction, so a crash between the two cannot leave a settled attempt
        with no recorded result.
        """
        if session is not None:
            self._settle_in(
                session, attempt_id, is_error, output_text, uncertain, verification_failed
            )
            return
        with self.db.session() as session:
            self._settle_in(
                session, attempt_id, is_error, output_text, uncertain, verification_failed
            )

    def _settle_in(
        self,
        session,
        attempt_id: str,
        is_error: bool,
        output_text: str,
        uncertain: bool,
        verification_failed: bool,
    ) -> None:
        from backend.models.tool_attempt import UNKNOWN

        repository = ToolAttemptRepository(session)
        attempt = repository.get(attempt_id)
        if attempt is None:
            return
        # A command that ran and failed verification is a settled failure, not a
        # completed change: resume must not tell the model to skip it.
        repository.set_status(
            attempt,
            UNKNOWN if uncertain else (FAILED if (is_error or verification_failed) else SUCCEEDED),
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

    def mark_reconciled(
        self, attempt_id: str, note: str = "Operator checked the current state"
    ) -> bool:
        from backend.models.tool_attempt import UNCERTAIN
        from backend.utils import utcnow

        with self.db.session() as session:
            repository = ToolAttemptRepository(session)
            attempt = repository.get(attempt_id)
            if attempt is None or attempt.status not in UNCERTAIN:
                return False
            attempt.reconciliation_note = note
            attempt.reconciled_at = utcnow()
            repository.set_status(attempt, RECONCILED)
            return True

    def uncertain_details(self, conversation_id: str) -> list[dict]:
        with self.db.session() as session:
            return [
                {
                    "id": a.id,
                    "run_id": a.run_id,
                    "tool_call_id": a.tool_call_id,
                    "tool": a.namespaced_name,
                    "effect": a.effect,
                    "status": a.status,
                    "arguments": json.loads(a.arguments_json or "{}"),
                    "arguments_digest": a.arguments_digest,
                    "started_at": a.started_at,
                    "ended_at": a.ended_at,
                }
                for a in ToolAttemptRepository(session).uncertain_for_conversation(conversation_id)
            ]

    def describe_conversation(self, conversation_id: str) -> dict:
        """The evidence a resume needs: what finished, what is uncertain."""
        with self.db.session() as session:
            repository = ToolAttemptRepository(session)
            completed = repository.settled_mutating_for_conversation(conversation_id)
            uncertain = repository.uncertain_for_conversation(conversation_id)
            return {
                "completed": [{"tool": a.namespaced_name, "effect": a.effect} for a in completed],
                "uncertain": [
                    {"id": a.id, "tool": a.namespaced_name, "status": a.status} for a in uncertain
                ],
            }
