"""Shared provider-neutral model catalogue and per-run context budgets."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from backend.services.providers.base import ModelInfo

FALLBACK_CONTEXT_WINDOW = 32_768  # a local budget, never advertised as model metadata


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
    output = min(settings.model_max_output_tokens, max(1, window // 4))
    return ModelBudget(window, output, source)


class ModelCatalogue:
    def __init__(self, provider, settings):
        self.provider = provider
        self.settings = settings
        self._models = None
        self._expires = 0
        self._lock = asyncio.Lock()
        self._error_until = 0

    async def list(self) -> list[ModelInfo]:
        async with self._lock:
            now = time.monotonic()
            if self._models is not None and now < self._expires:
                return self._models
            if now < self._error_until:
                raise RuntimeError("model catalogue temporarily unavailable")
            try:
                async with asyncio.timeout(8):
                    models = await self.provider.list_models()
            except Exception:
                self._error_until = time.monotonic() + 30
                raise
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
