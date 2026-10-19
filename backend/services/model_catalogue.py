"""Shared provider-neutral model catalogue and per-run context budgets."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from backend.services.providers.base import ModelInfo, ProviderError

FALLBACK_CONTEXT_WINDOW = 32_768  # a local budget, never advertised as model metadata
# Ceiling for the derived output allowance. Half the window leaves the other half for
# the prompt, which is generous for an agent that writes files but still bounded.
ADAPTIVE_OUTPUT_CEILING = 32_768
# An accepted configuration must leave room for a minimal prompt. An output cap is
# an upper bound, not a requirement to reserve it all when the prompt is smaller.
MIN_INPUT_TOKENS = 512


@dataclass(frozen=True)
class ModelBudget:
    context_window: int
    max_output_tokens: int
    source: str

    @property
    def input_tokens(self) -> int:
        # Local token estimates are approximate; leave 5% headroom as well as output.
        return max(
            1, self.context_window - self.max_output_tokens - max(64, self.context_window // 20)
        )


def budget_for(settings, info: ModelInfo | None = None) -> ModelBudget:
    reported = info.context_window if info else None
    override = settings.model_context_window
    if override:
        window = min(override, reported) if reported else override
        source = "override" if not reported or override < reported else "provider"
    else:
        window, source = (
            (reported, "provider") if reported else (FALLBACK_CONTEXT_WINDOW, "fallback")
        )
    # An explicit setting is a hard cap the operator chose. Blank means adaptive:
    # take half the window, up to a ceiling. A fixed number cannot be right for both
    # a reasoning model that spends its budget thinking and a model that answers
    # directly, and guessing one is what made a normal turn stop with no output.
    headroom = max(64, window // 20)
    # Allocate jointly: the output cap cannot consume the prompt's minimum, and a
    # small window cannot give all of itself to one side.
    max_output = max(1, window - headroom - MIN_INPUT_TOKENS)
    balanced_output = max(1, window // 2)
    if settings.model_max_output_tokens:
        output = min(
            settings.model_max_output_tokens, max_output, balanced_output
        )
    else:
        output = min(ADAPTIVE_OUTPUT_CEILING, balanced_output, max_output)
    return ModelBudget(window, output, source)


class ModelCatalogue:
    def __init__(self, provider, settings):
        self.provider = provider
        self.settings = settings
        self._models = None
        self._expires = 0
        self._lock = asyncio.Lock()
        self._error_until = 0
        self._failure = None

    async def list(self) -> list[ModelInfo]:
        async with self._lock:
            now = time.monotonic()
            if self._models is not None and now < self._expires:
                return self._models
            if now < self._error_until:
                message, status, category = self._failure
                raise ProviderError(message, status=status, category=category)
            try:
                async with asyncio.timeout(8):
                    models = await self.provider.list_models()
            except Exception as exc:
                self._error_until = time.monotonic() + 30
                if isinstance(exc, ProviderError):
                    self._failure = (str(exc), exc.status, exc.category)
                    raise
                status = 504 if isinstance(exc, TimeoutError) else 503
                self._failure = (
                    "Model catalogue temporarily unavailable.", status, "provider_unavailable"
                )
                raise ProviderError(
                    self._failure[0], status=status, category=self._failure[2]
                ) from exc
            self._models = models
            self._expires = time.monotonic() + 300
            return models

    async def budget(self, model_id: str) -> ModelBudget:
        try:
            models = await self.list()
        except Exception:
            models = []
        info = next((item for item in models if item.id == model_id), None)
        return budget_for(self.settings, info)
