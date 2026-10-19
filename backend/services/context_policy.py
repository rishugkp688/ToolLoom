"""One coordinator for fitting a prompt into the model's window.

The earlier design split this across two components that did not know about each
other: a synchronous builder that trimmed by whole user turns, and a compactor that
ran once before the loop. Neither could help the case that actually breaks — a long
single turn of tool calls — so the run died at the budget instead of shedding old
work.

The ladder here is ordered by what each step costs:

1. **Fit.** Nothing to do, and the provider's own token count can be trusted.
2. **Prune old tool results.** Free, and the action history stays legible because the
   ``tool_use`` blocks are untouched.
3. **Summarise.** Costs one model call, but keeps the facts.
4. **Drop the oldest exchanges.** The only step that truly loses information.
5. **Refuse.** If the system prompt, the tool schemas and the newest exchange alone do
   not fit, there is no lossless way forward and saying so beats truncating the
   operator's message.

Steps 2 and 4 happen inside the builder; step 3 is asynchronous and lives here.
"""

from __future__ import annotations

from backend.services.context_builder import (
    BuiltContext,
    ContextBudgetError,
    ContextBuilder,
    UsageAnchor,
)
from backend.utils.async_work import run_blocking


class ContextPolicy:
    def __init__(self, db, settings, compactor=None) -> None:
        self.db = db
        self.settings = settings
        self.compactor = compactor

    async def prepare(
        self,
        conversation_id: str,
        *,
        advisory: str | None = None,
        pinned: str = "",
        anchor: UsageAnchor | None = None,
        model_id: str | None = None,
        budget=None,
    ) -> tuple[BuiltContext, str | None]:
        """Return a prompt that fits, plus a note about what was done to get there.

        The note is ``None`` when nothing was reduced, otherwise ``"summarised"`` or
        ``"summarised+trimmed"``, so a run can say honestly what it lost rather than
        implying it saw the whole history.
        """
        builder = ContextBuilder(self.db, self.settings, budget)
        try:
            built = await run_blocking(
                builder.build,
                conversation_id,
                advisory=advisory,
                pinned=pinned,
                anchor=anchor,
                allow_drop=False,
            )
        except ContextBudgetError:
            pass
        else:
            # Pruning is a reduction too, and the caller should be able to say so
            # rather than reporting that nothing happened.
            return built, ("pruned" if built.pruned_results or built.projected_exchanges else None)

        if self.compactor is None:
            # No summariser available, so fall through to trimming.
            return await run_blocking(
                builder.build, conversation_id, advisory=advisory, pinned=pinned, anchor=anchor
            ), "trimmed"

        # Summarising preserves facts that dropping would lose, so it goes first.
        # A failure here is not fatal: the builder below still trims.
        summarised = await self.compactor.compact_if_needed(
            conversation_id, model_id=model_id, budget=budget, force=True
        )
        built = await run_blocking(
            builder.build, conversation_id, advisory=advisory, pinned=pinned, anchor=anchor
        )
        if built.dropped_units or built.omitted_rows or built.projected_exchanges:
            return built, "summarised+trimmed" if summarised else "trimmed"
        return built, "summarised" if summarised else "trimmed"
