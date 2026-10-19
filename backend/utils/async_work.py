"""Run short blocking operations without blocking or abandoning their transactions."""

from __future__ import annotations

import asyncio
from functools import partial

import anyio


async def run_blocking(fn, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(partial(fn, *args, **kwargs)))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Transactions/atomic publication cannot be interrupted halfway by losing
        # their awaiting request. Wait off-loop through repeated/level cancellation.
        with anyio.CancelScope(shield=True):
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
        if not task.cancelled():
            task.exception()
        raise
