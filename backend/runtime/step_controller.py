"""Deterministic per-turn loop guards.

Detects duplicate calls, consecutive-error cascades and call floods. It cannot
grant tool access — it only refuses calls it has already seen fail repeatedly and
emits advisory notes the runtime can surface to the model.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

DUP_THRESHOLD = 3
ERROR_CASCADE_THRESHOLD = 3
FLOOD_THRESHOLD = 6
MAX_ADVICE = 3


@dataclass
class StepNote:
    level: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)


def _signature(arguments: dict[str, Any]) -> str:
    try:
        payload = json.dumps(arguments or {}, sort_keys=True, default=str)
    except (TypeError, ValueError):
        payload = repr(arguments)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


class StepController:
    def __init__(self) -> None:
        self._calls: list[tuple[str, str]] = []
        self._error_streak: dict[str, int] = {}
        self._total_calls = 0
        self._had_text = False
        self._advice: list[str] = []

    def note_text(self) -> None:
        self._had_text = True

    def note_call(self, name: str, arguments: dict[str, Any]) -> list[StepNote]:
        signature = _signature(arguments)
        self._calls.append((name, signature))
        self._total_calls += 1
        notes: list[StepNote] = []

        repeats = sum(1 for n, s in self._calls if n == name and s == signature)
        if repeats == DUP_THRESHOLD:
            note = StepNote(
                "warn",
                f"`{name}` called with identical arguments {DUP_THRESHOLD} times this turn",
                {"tool": name},
            )
            notes.append(note)
        if self._total_calls == FLOOD_THRESHOLD and not self._had_text:
            notes.append(
                StepNote(
                    "warn",
                    f"{FLOOD_THRESHOLD} tool calls without a user-facing answer yet — "
                    "answer with what you have if possible",
                    {},
                )
            )
        self._remember(notes)
        return notes

    def note_result(self, name: str, is_error: bool) -> StepNote | None:
        if is_error:
            self._error_streak[name] = self._error_streak.get(name, 0) + 1
        else:
            self._error_streak[name] = 0
        if self._error_streak[name] == ERROR_CASCADE_THRESHOLD:
            note = StepNote(
                "warn",
                f"`{name}` has failed {ERROR_CASCADE_THRESHOLD} times in a row this turn",
                {"tool": name},
            )
            self._remember([note])
            return note
        return None

    def should_refuse(self, name: str, arguments: dict | None = None) -> bool:
        repeated = (
            arguments is not None
            and self._calls.count((name, _signature(arguments))) >= DUP_THRESHOLD
        )
        return repeated or self._error_streak.get(name, 0) >= ERROR_CASCADE_THRESHOLD

    def advice(self) -> str | None:
        if not self._advice:
            return None
        return (
            "<loop_guards>\n" + "\n".join(f"- {item}" for item in self._advice) + "\n</loop_guards>"
        )

    def _remember(self, notes: list[StepNote]) -> None:
        for note in notes:
            self._advice.append(note.message)
        self._advice = self._advice[-MAX_ADVICE:]
