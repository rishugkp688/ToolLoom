"""Rolling conversation compaction, performed through the selected provider.

Never makes a hidden provider call: it uses the same ``LLMProvider`` as the chat
turn. Failures are non-fatal — the context builder's budget trim still applies.
"""

from __future__ import annotations

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
    ) -> bool:
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
            if pending_tokens + len(previous) // 4 < int(budget.input_tokens * 0.75):
                return False
            cutoff = _cutoff_index(pending, max(1, int(budget.input_tokens * 0.3)))
            if cutoff <= 0:
                return False
            head = pending[:cutoff]
            head_text = _render(head)

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
        return turn.text.strip()[: min(MAX_SUMMARY_TOKENS, budget.max_output_tokens) * 4]


def _to_messages(rows) -> list:
    from backend.services.providers.base import ProviderMessage, TextBlock

    return [ProviderMessage("user", [TextBlock(row.content or "")]) for row in rows]


def _render(rows) -> str:
    lines = []
    for row in rows:
        lines.append(f"[{row.role}] {row.content}")
    return "\n".join(lines)


def _cutoff_index(pending: list, recent_tokens: int) -> int:
    """Return the index where the kept (recent) window starts.

    Leaves the model-specific recent token budget and snaps back to a user message so
    the newest turn is never folded into the summary.
    """
    if not pending:
        return 0
    index = len(pending) - 1
    keep_tokens = estimate_tokens(_to_messages([pending[index]]), None, [])
    while index > 0 and keep_tokens < recent_tokens:
        index -= 1
        keep_tokens += estimate_tokens(_to_messages([pending[index]]), None, [])
    while index > 0 and pending[index].role != "user":
        index -= 1
    if index <= 0 or index >= len(pending):
        return 0
    return index


__all__ = ["ContextCompactor"]
