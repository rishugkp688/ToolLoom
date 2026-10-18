"""Rolling conversation compaction, performed through the selected provider.

Never makes a hidden provider call: it uses the same ``LLMProvider`` as the chat
turn. Failures are non-fatal — the context builder's budget trim still applies.
"""

from __future__ import annotations

import json

from backend.repositories import ConversationRepository, MessageRepository
from backend.services.context_builder import estimate_tokens
from backend.services.model_catalogue import ModelBudget, budget_for

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
        with self.db.session() as session:
            conversation = ConversationRepository(session).get(conversation_id)
            if conversation is None:
                return False
            rows = MessageRepository(session).list_by_conversation(conversation_id)
            summary_count = conversation.summary_message_count or 0
            pending = rows[summary_count:]
            pending_tokens = estimate_tokens(_to_messages(pending), None, [])
            previous = conversation.summary or ""
            if not force and pending_tokens + len(previous) // 4 < int(budget.input_tokens * 0.75):
                return False
            cutoff = _cutoff_index(pending, max(1, int(budget.input_tokens * 0.3)))
            if cutoff <= 0:
                return False
            head = pending[:cutoff]
            # Advance the cursor only over messages the summarizer can actually
            # receive. Truncating the input and then marking the whole head as
            # summarised silently loses the tail from every future context.
            max_chars = max(1, _summary_input_chars(budget) - len(previous) - 32)
            while len(head) > 1 and len(_render(head)) > max_chars:
                head.pop()
            if not head:
                return False
            head_text = _render(head)
            if len(head_text) > max_chars:
                return False

        summary = await self._summarize(previous, head_text, model_id, budget)
        if not summary:
            return False

        with self.db.session() as session:
            conversation = ConversationRepository(session).get(conversation_id)
            if conversation is None:
                return False
            conversation.summary = summary
            conversation.summary_message_count = summary_count + len(head)
            session.flush()
        return True

    async def _summarize(self, previous: str, head_text: str, model_id, budget: ModelBudget) -> str:
        from backend.services.providers.base import (
            BaseProvider,
            ProviderMessage,
            TextBlock,
        )

        provider: BaseProvider = self.provider
        prompt = head_text
        if previous:
            prompt = f"Existing summary:\n{previous}\n\nNewer messages:\n{head_text}"
        max_chars = max(1, (budget.input_tokens - len(SUMMARY_PROMPT) // 4 - 64) * 3)
        if len(prompt) > max_chars:
            prompt = prompt[:max_chars] + "\n[summary input excerpt truncated]"
        try:
            turn = await provider.complete(
                messages=[ProviderMessage("user", [TextBlock(prompt)])],
                system=SUMMARY_PROMPT,
                model_id=model_id or getattr(provider, "model_id", None),
                max_tokens=min(MAX_SUMMARY_TOKENS, budget.max_output_tokens),
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
        return text[: min(MAX_SUMMARY_TOKENS, budget.max_output_tokens) * 4]


def _to_messages(rows) -> list:
    from backend.services.providers.base import ProviderMessage, TextBlock

    return [ProviderMessage("user", [TextBlock(row.content or "")]) for row in rows]


def _blocks(row) -> list[dict]:
    try:
        blocks = json.loads(row.content_blocks_json or "[]")
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
