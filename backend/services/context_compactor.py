"""Rolling conversation compaction, performed through the selected provider.

Never makes a hidden provider call: it uses the same ``LLMProvider`` as the chat
turn. Failures are non-fatal — the context builder's budget trim still applies.
"""

from __future__ import annotations

import json

from backend.repositories import ConversationRepository, MessageRepository
from backend.services.context_builder import estimate_tokens
from backend.services.model_catalogue import ModelBudget, budget_for
from backend.utils.async_work import run_blocking

MAX_SUMMARY_TOKENS = 1_500

SUMMARY_PROMPT = (
    "Summarize the conversation above for future context. Preserve every fact that "
    "matters: names, numbers, IDs, URLs, file paths, decisions, tool calls made and "
    "their outcomes, and any explicit user instructions. Do not invent details. Do "
    "not add filler. Write compact prose or bullets."
)


class ContextCompactor:
    def __init__(self, db, settings, provider) -> None:
        self.db = db
        self.settings = settings
        self.provider = provider

    async def compact_if_needed(
        self,
        conversation_id: str,
        *,
        model_id: str | None = None,
        budget: ModelBudget | None = None,
        force: bool = False,
    ) -> bool:
        """Summarise older messages into the conversation's rolling summary.

        ``force`` bypasses the pressure threshold. The context policy uses it when the
        prompt cannot be fitted even after pruning, because summarising keeps the facts
        whereas shedding exchanges loses them; it should therefore be tried first.
        """
        budget = budget or budget_for(self.settings)
        prepared = await run_blocking(self._prepare_head, conversation_id, budget, force)
        if prepared is None:
            return False
        previous, head_text, summary_count, head_count = prepared

        summary = await self._summarize(previous, head_text, model_id, budget)
        if not summary:
            return False

        return await run_blocking(
            self._store_summary, conversation_id, summary_count, head_count, summary
        )

    def _prepare_head(self, conversation_id, budget, force):
        with self.db.session() as session:
            conversation = ConversationRepository(session).get(conversation_id)
            if conversation is None:
                return None
            summary_count = conversation.summary_message_count or 0
            # Already-summarised rows cannot affect the next summary.
            pending = MessageRepository(session).list_from(conversation_id, offset=summary_count)
            pending_tokens = estimate_tokens(_to_messages(pending), None, [])
            previous = conversation.summary or ""
            if not force and pending_tokens + len(previous) // 4 < int(budget.input_tokens * 0.75):
                return None
            cutoff = _cutoff_index(pending, max(1, int(budget.input_tokens * 0.3)))
            # Shrink in complete exchange units. Popping individual rows could
            # summarise an assistant call while leaving its result in the retained
            # suffix — an orphan every strict provider rejects.
            cutoff = _snap_to_boundary(pending, cutoff)
            if cutoff <= 0:
                return None
            units = _units(pending[:cutoff])
            # Advance the cursor only over messages the summarizer can actually
            # receive. Truncating the input and then marking the whole head as
            # summarised silently loses the tail from every future context.
            max_chars = max(0, _summary_input_chars(budget) - len(_summary_input(previous, "")))
            while units and len(_render(_flatten(units))) > max_chars:
                units.pop()
            if not units:
                return None
            head = _flatten(units)
            head_text = _render(head)

        return previous, head_text, summary_count, len(head)

    def _store_summary(self, conversation_id, summary_count, head_count, summary):
        with self.db.session() as session:
            conversation = ConversationRepository(session).get(conversation_id)
            if conversation is None or (conversation.summary_message_count or 0) != summary_count:
                return False
            conversation.summary = summary
            conversation.summary_message_count = summary_count + head_count
            session.flush()
        return True

    async def _summarize(self, previous: str, head_text: str, model_id, budget: ModelBudget) -> str:
        from backend.services.providers.base import (
            BaseProvider,
            ProviderMessage,
            TextBlock,
        )

        provider: BaseProvider = self.provider
        prompt = _summary_input(previous, head_text)
        if len(prompt) > _summary_input_chars(budget):
            return ""
        output_limit = min(MAX_SUMMARY_TOKENS, budget.max_output_tokens)
        try:
            turn = await provider.complete(
                messages=[ProviderMessage("user", [TextBlock(prompt)])],
                system=SUMMARY_PROMPT,
                model_id=model_id or getattr(provider, "model_id", None),
                max_tokens=output_limit,
            )
        except Exception:
            return ""
        if not turn:
            return ""
        # A truncated or failed summary must never advance the cursor, because the
        # messages it covers then leave the model's context for good. Only a
        # completed turn is safe to treat as the record of what they said.
        if turn.stop_reason != "end_turn":
            return ""
        text = turn.text.strip()
        if not text:
            return ""
        # The output allowance is a contract. If the provider ignores it, the
        # summary may not cover everything it was given, so reject it rather than
        # slice the text and advance the cursor as though it did.
        if turn.usage.output_tokens and turn.usage.output_tokens > output_limit:
            return ""
        if len(text) > output_limit * 4:
            return ""
        return text


def _to_messages(rows) -> list:
    from backend.services.providers.base import ProviderMessage, TextBlock

    return [ProviderMessage("user", [TextBlock(row.content or "")]) for row in rows]


def _blocks(row) -> list[dict]:
    try:
        blocks = json.loads(getattr(row, "content_blocks_json", None) or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(blocks, list):
        return []
    return [block for block in blocks if isinstance(block, dict)]


def _render(rows) -> str:
    """Render rows for summarisation without dropping the action history.

    Assistant rows that carry only tool calls have empty ``content``, so rendering
    content alone would lose which tools were called and with what arguments —
    exactly the facts a long task needs in order to continue.
    """
    lines = []
    for row in rows:
        parts = [f"[{row.role}]"]
        if getattr(row, "tool_name", None):
            parts.append(f"({row.tool_name})")
        text = (row.content or "").strip()
        if text:
            parts.append(text)
        for block in _blocks(row):
            kind = block.get("type")
            if kind == "tool_use":
                arguments = json.dumps(block.get("arguments") or {}, sort_keys=True)
                parts.append(f"called {block.get('name')} with {arguments}")
            elif kind == "tool_result" and block.get("is_error"):
                parts.append("(tool returned an error)")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def _summary_input_chars(budget: ModelBudget) -> int:
    return max(1, (budget.input_tokens - len(SUMMARY_PROMPT) // 4 - 64) * 3)


def _summary_input(previous: str, head: str) -> str:
    return f"Existing summary:\n{previous}\n\nNewer messages:\n{head}" if previous else head


def _row_has_tool_use(row) -> bool:
    return any(block.get("type") == "tool_use" for block in _blocks(row))


def _units(rows: list) -> list[list]:
    """Group rows into complete exchanges: a call together with its results."""
    units: list[list] = []
    for row in rows:
        if row.role == "tool" and units and any(_row_has_tool_use(item) for item in units[-1]):
            units[-1].append(row)
        else:
            units.append([row])
    return units


def _flatten(units: list[list]) -> list:
    return [row for unit in units for row in unit]


def _snap_to_boundary(rows: list, index: int) -> int:
    """The largest exchange boundary at or before ``index``."""
    boundary = 0
    for unit in _units(rows):
        next_boundary = boundary + len(unit)
        if next_boundary > index:
            break
        boundary = next_boundary
    return boundary


def _cutoff_index(pending: list, recent_tokens: int) -> int:
    """Return the index where the kept (recent) window starts.

    Leaves the model-specific recent token budget and snaps back so the kept suffix
    never starts with a tool result — a result whose call was summarised away is an
    orphan a provider rejects.

    It deliberately does *not* require a ``user`` row. A single long task has exactly
    one, at the very start, so the earlier version returned cutoff 0 and compaction
    never ran for precisely the workflow it exists to support.
    """
    if not pending:
        return 0
    index = len(pending) - 1
    keep_tokens = estimate_tokens(_to_messages([pending[index]]), None, [])
    while index > 0 and keep_tokens < recent_tokens:
        index -= 1
        keep_tokens += estimate_tokens(_to_messages([pending[index]]), None, [])
    while index > 0 and pending[index].role == "tool":
        index -= 1
    if index <= 0 or index >= len(pending):
        return 0
    return index


__all__ = ["ContextCompactor"]
