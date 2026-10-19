"""Token usage aggregation (and an optional cost estimate)."""

from __future__ import annotations

from backend.repositories import ConversationRepository, MessageRepository


class UsageService:
    def __init__(self, db, settings) -> None:
        self.db = db
        self.settings = settings

    def summary(self) -> dict:
        with self.db.session() as session:
            rows = MessageRepository(session).usage_by_conversation()
            title_usage = {
                row.id: (
                    (row.title_usage_input + row.compaction_usage_input
                     + row.interrupted_usage_input),
                    (row.title_usage_output + row.compaction_usage_output
                     + row.interrupted_usage_output),
                )
                for row in ConversationRepository(session).list()
            }

        per_conversation: dict[str, dict] = {}
        total_in = total_out = 0
        for conversation_id, usage_in, usage_out in rows:
            extra_in, extra_out = title_usage.get(conversation_id, (0, 0))
            usage_in += extra_in
            usage_out += extra_out
            if not usage_in and not usage_out:
                continue
            total_in += usage_in
            total_out += usage_out
            per_conversation[conversation_id] = {
                "input_tokens": usage_in,
                "output_tokens": usage_out,
                "total_tokens": usage_in + usage_out,
                "estimated_cost": self.settings.estimated_cost(usage_in, usage_out),
            }

        return {
            "accounting": {
                "basis": "provider_reported",
                "includes": ["transcript", "titles", "compaction", "reported_interrupted_usage"],
                "limitation": "Requests with no provider usage report cannot be counted exactly.",
                "context_budgeting": "character estimate calibrated by provider prompt usage",
            },
            "global_": {
                "input_tokens": total_in,
                "output_tokens": total_out,
                "total_tokens": total_in + total_out,
                "estimated_cost": self.settings.estimated_cost(total_in, total_out),
            },
            "per_conversation": per_conversation,
        }
