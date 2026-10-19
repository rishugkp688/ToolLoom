"""Transport dispatch for MCP servers.

Supports the in-process ``local`` transport (the built-in sandbox), the
in-process ``generated`` transport (installed Tool Foundry capabilities), remote
``http`` (Streamable HTTP) and ``sse`` (legacy) transports, and operator-managed
``stdio``. All of them present the same capability model: discovery through the
registry, namespaced tool names, schema validation and one executor.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from functools import partial
from typing import Any
from weakref import WeakKeyDictionary, WeakSet

import anyio

from backend.repositories.mcp_repository import MCPServerRepository
from backend.services.mcp_client import (
    MCPError,
    ToolCallOutcome,
    call_tool_on,
    list_tools_on,
    open_session,
)
from backend.services.sandbox import dispatch as sandbox_dispatch
from backend.services.sandbox import effects_for as sandbox_effects_for
from backend.services.sandbox import tools_for as sandbox_tools_for

__all__ = ["MCPConnectionManager", "MCPError", "ToolCallOutcome"]

# Mutating local tools run in a worker thread. Cancelling the awaiting coroutine
# does not stop that thread, so ownership must remain held until it actually
# finishes; otherwise an older write can publish over a newer one.
_MUTATION_LOCKS: WeakKeyDictionary = WeakKeyDictionary()
_WORKERS: set[asyncio.Task] = set()


def _locks_for_loop() -> dict[str, asyncio.Lock]:
    loop = asyncio.get_running_loop()
    locks = _MUTATION_LOCKS.get(loop)
    if locks is None:
        locks = {}
        _MUTATION_LOCKS[loop] = locks
    return locks


async def _run_serialized(key: str, fn, *args):
    lock = _locks_for_loop().setdefault(key, asyncio.Lock())
    started = False

    async def owner():
        nonlocal started
        async with lock:
            # This task, rather than the request, owns the lease until the actual
            # thread returns. Repeated/level cancellation cannot release it early.
            started = True
            return await asyncio.to_thread(fn, *args)

    task = asyncio.create_task(owner())
    _WORKERS.add(task)
    task.add_done_callback(_WORKERS.discard)
    task.add_done_callback(_consume_exception)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if not started:
            task.cancel()  # A cancelled queued request must never start later.
        raise


def _async_local_handler(server_name: str, tool_name: str):
    """Local tools that must run on the event loop because they spawn children.

    The rest of the local tools are synchronous and run in a worker thread.
    """
    if server_name != "sandbox":
        return None
    if tool_name == "python":
        from backend.services.sandbox.python_tool import execute

        return execute
    if tool_name == "shell":
        from backend.services.sandbox.shell_tool import execute

        return execute
    from backend.services.sandbox.foundry_tools import ASYNC_HANDLERS

    return ASYNC_HANDLERS.get((server_name, tool_name))


def _consume_exception(task: asyncio.Task) -> None:
    """Retrieve a failed owner task's exception so asyncio does not warn."""
    if not task.cancelled():
        task.exception()


class _SessionHandle:
    """One supervised MCP session owned by a dedicated task.

    The SDK's client context is bound to the task that entered it, so a session
    cannot simply be stored and reused from another request task. This actor owns
    the context for its whole life and executes requests submitted to its queue.
    """

    def __init__(self, opener) -> None:
        self._opener = opener
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=16)
        self._ready: asyncio.Future = asyncio.get_running_loop().create_future()
        self._ready.add_done_callback(_consume_exception)
        self._task = asyncio.create_task(self._run())
        self._task.add_done_callback(_consume_exception)

    @property
    def done(self) -> bool:
        return self._task.done()

    async def ready(self) -> None:
        await asyncio.shield(self._ready)

    def _fail_pending(self, exc: BaseException) -> None:
        while not self._queue.empty():
            item = self._queue.get_nowait()
            if item is None:
                continue
            future, _ = item
            if not future.done():
                future.set_exception(exc)

    async def _run(self) -> None:
        try:
            async with self._opener() as session:
                if self._ready.done():
                    return
                self._ready.set_result(session)
                while True:
                    item = await self._queue.get()
                    if item is None:
                        break
                    future, fn = item
                    if future.done():
                        continue
                    try:
                        value = await fn(session)
                    except asyncio.CancelledError:
                        if not future.done():
                            future.cancel()
                        raise
                    except Exception as exc:
                        if not future.done():
                            future.set_exception(exc)
                    else:
                        if not future.done():
                            future.set_result(value)
        except asyncio.CancelledError:
            if not self._ready.done():
                self._ready.cancel()
            self._fail_pending(asyncio.CancelledError())
            raise
        except Exception as exc:
            if not self._ready.done():
                self._ready.set_exception(exc)
            self._fail_pending(exc)

    async def run(self, fn):
        await self.ready()
        if self._task.done():
            raise MCPError("MCP session is no longer running", stage="cleanup")
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        await self._queue.put((future, fn))
        # Wait for either the call or the owner task: if the server exits mid-call,
        # waiting only on the future would hang forever.
        try:
            done, _ = await asyncio.wait({future, self._task}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            future.cancel()
            raise
        if future in done:
            return future.result()
        if not future.done():
            future.cancel()
        raise MCPError("MCP session stopped before the call completed", stage="cleanup")

    async def close(self) -> None:
        if self._task.done():
            return
        self._task.cancel()
        # Cleanup is owned by the session task even if this waiter is cancelled.
        # Do not queue behind a hung operation, or await unbounded cancellation.
        with anyio.CancelScope(shield=True):
            await asyncio.wait({self._task}, timeout=3)


class _SessionPool:
    """Supervised sessions that outlive one tool call.

    Stateful stdio, HTTP and SSE servers can keep counters, handles and open
    documents in a session. Entries are keyed by server identity plus configuration
    and closed on invalidation or shutdown. Failed operations are never replayed here.
    """

    def __init__(self) -> None:
        # Sessions belong to the event loop that opened them. A loop-scoped map
        # keeps a test or a restarted app from reusing a session from a dead loop.
        self._entries: WeakKeyDictionary = WeakKeyDictionary()
        self._locks: WeakKeyDictionary = WeakKeyDictionary()

    def _for_loop(self) -> dict[str, _SessionHandle]:
        loop = asyncio.get_running_loop()
        entries = self._entries.get(loop)
        if entries is None:
            entries = {}
            self._entries[loop] = entries
        return entries

    def _locks_for_loop(self) -> dict[str, asyncio.Lock]:
        loop = asyncio.get_running_loop()
        locks = self._locks.get(loop)
        if locks is None:
            locks = {}
            self._locks[loop] = locks
        return locks

    async def session(self, key: str, opener) -> _SessionHandle:
        entries = self._for_loop()
        lock = self._locks_for_loop().setdefault(key, asyncio.Lock())
        async with lock:
            entry = entries.get(key)
            if entry is not None and not entry.done:
                return entry
            handle = _SessionHandle(opener)
            entries[key] = handle
            try:
                await handle.ready()
            except BaseException:
                entries.pop(key, None)
                await handle.close()
                raise
            return handle

    async def invalidate(self, key: str) -> None:
        entry = self._for_loop().pop(key, None)
        if entry is not None:
            await entry.close()

    async def invalidate_server(self, server_id: str) -> None:
        entries = self._for_loop()
        prefix = f"{server_id}:"
        for key in [key for key in entries if key.startswith(prefix)]:
            await self.invalidate(key)

    async def aclose(self) -> None:
        entries = self._for_loop()
        for key in list(entries):
            await self.invalidate(key)
        self._entries.pop(asyncio.get_running_loop(), None)
        self._locks.pop(asyncio.get_running_loop(), None)


class MCPConnectionManager:
    def __init__(self, settings, secret_resolver=None) -> None:
        self.settings = settings
        self.secret_resolver = secret_resolver
        self._pool = _SessionPool()
        self._invalidations: set[asyncio.Task] = set()
        self._owner_loops: WeakSet = WeakSet()

    @staticmethod
    def workspace_busy(root) -> bool:
        key = str(root)
        return any(
            locks.get(key) is not None and locks[key].locked()
            for locks in list(_MUTATION_LOCKS.values())
        )

    # -- headers --------------------------------------------------------------

    def _headers(self, server) -> dict[str, str]:
        headers = MCPServerRepository.headers_for(server)
        if self.secret_resolver is not None and hasattr(self.secret_resolver, "resolve_mapping"):
            headers = self.secret_resolver.resolve_mapping(headers)
        return headers

    # -- discovery ------------------------------------------------------------

    def local_tools(self, server) -> list[dict[str, Any]]:
        effects = sandbox_effects_for(server.name)
        return [
            {
                "name": tool.name,
                "server_name": server.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
                "risk": tool.risk,
                "effect": effects.get(tool.name, "unknown"),
            }
            for tool in sandbox_tools_for(server.name)
        ]

    def _stdio_kwargs(self, server) -> dict[str, Any]:
        if not self.settings.toolloom_allow_stdio:
            raise MCPError(
                "stdio MCP servers are disabled; set TOOLLOOM_ALLOW_STDIO=1 to allow "
                "operator-configured local processes"
            )
        if not server.command:
            raise MCPError(f"stdio server {server.name!r} has no command configured")
        try:
            args = json.loads(server.args_json or "[]")
        except json.JSONDecodeError:
            args = []
        env = json.loads(server.env_json or "{}")
        if self.secret_resolver is not None:
            env = self.secret_resolver.resolve_mapping(env)
        return {"command": server.command, "args": args, "cwd": server.cwd, "env": env}

    def _session_key(self, server) -> str:
        transport = getattr(server, "transport", "http")
        extra = self._stdio_kwargs(server) if transport == "stdio" else {}
        material = json.dumps(
            {
                "transport": transport,
                "url": server.url,
                "headers": self._headers(server),
                "timeout_ms": server.timeout_ms,
                "verify_ssl": server.verify_ssl,
                **extra,
            },
            sort_keys=True,
            default=str,
        )
        return f"{server.id}:{hashlib.sha256(material.encode('utf-8')).hexdigest()[:16]}"

    async def _pooled_session(self, server):
        self._owner_loops.add(asyncio.get_running_loop())
        transport = getattr(server, "transport", "http")
        extra = self._stdio_kwargs(server) if transport == "stdio" else {}
        opener = partial(
            open_session,
            url=server.url,
            headers=self._headers(server),
            timeout_ms=server.timeout_ms,
            verify_ssl=server.verify_ssl,
            transport=transport,
            deadline=False,
            **extra,
        )
        return await self._pool.session(self._session_key(server), opener)

    async def invalidate_server(self, server_id: str) -> None:
        await self._pool.invalidate_server(server_id)

    def invalidate_server_soon(self, server_id: str) -> None:
        """Close a server's pooled sessions when the caller is synchronous.

        Configuration changes get a new key anyway, so an old session can never be
        reused; this just releases its process promptly when a loop is available.
        """

        async def invalidate_keys(keys):
            for key in keys:
                await self._pool.invalidate(key)

        def schedule(keys):
            task = asyncio.create_task(invalidate_keys(keys))
            self._invalidations.add(task)
            task.add_done_callback(self._invalidations.discard)
            task.add_done_callback(_consume_exception)

        # Registry mutations may run in a FastAPI worker thread. Schedule on the
        # loop that owns each SDK session rather than silently doing nothing.
        for loop in list(self._owner_loops):
            if not loop.is_closed():
                # Snapshot the superseded revision now. A delayed invalidation
                # must not close a new configuration opened immediately after it.
                keys = [
                    key
                    for key in self._pool._entries.get(loop, {})
                    if key.startswith(f"{server_id}:")
                ]
                loop.call_soon_threadsafe(schedule, keys)

    async def aclose(self) -> None:
        pending = [t for t in self._invalidations if t.get_loop() is asyncio.get_running_loop()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await self._pool.aclose()

    async def discover_tools(self, server) -> list[dict[str, Any]]:
        transport = getattr(server, "transport", "http")
        if transport == "local":
            return self.local_tools(server)
        if transport == "generated":
            from backend.services.tool_foundry import registry_adapter

            return registry_adapter.discover(self.settings)
        if transport in {"http", "sse", "stdio"}:
            if transport in {"http", "sse"} and not server.url:
                raise MCPError(f"server {server.name!r} has no url")
            key = self._session_key(server)

            deadline = asyncio.get_running_loop().time() + server.timeout_ms / 1000

            async def discover(session):
                async with asyncio.timeout_at(deadline):
                    return await list_tools_on(session)

            try:
                async with asyncio.timeout_at(deadline):
                    handle = await self._pooled_session(server)
                    tools = await handle.run(discover)
            except asyncio.CancelledError:
                await self._pool.invalidate(key)
                raise
            except MCPError:
                await self._pool.invalidate(key)
                raise
            except Exception as exc:
                await self._pool.invalidate(key)
                raise MCPError(str(exc), stage="dispatch") from exc
            return [
                {
                    "name": tool.name,
                    "server_name": server.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
                for tool in tools
            ]
        raise MCPError(f"unsupported transport {transport!r}")

    # -- dispatch -------------------------------------------------------------

    async def call_tool(
        self,
        server,
        tool_name: str,
        arguments: dict[str, Any],
        ctx,
    ) -> ToolCallOutcome:
        transport = getattr(server, "transport", "http")
        if transport == "local":
            handler = _async_local_handler(server.name, tool_name)
            if handler is not None:
                lock = _locks_for_loop().setdefault(str(ctx.workspace().root), asyncio.Lock())
                async with lock:
                    return await handler(arguments, ctx, self.settings)
            # Local handlers may do blocking I/O (e.g. http_fetch); keep the loop free.
            effect = sandbox_effects_for(server.name).get(tool_name, "unknown")
            if effect != "read_only":
                text = await _run_serialized(
                    str(ctx.workspace().root),
                    sandbox_dispatch,
                    server.name,
                    tool_name,
                    arguments,
                    ctx,
                )
            else:
                text = await asyncio.to_thread(
                    sandbox_dispatch, server.name, tool_name, arguments, ctx
                )
            return ToolCallOutcome(text=text, is_error=False)
        if transport == "generated":
            from backend.services.tool_foundry import registry_adapter

            lock = _locks_for_loop().setdefault(str(ctx.workspace().root), asyncio.Lock())
            async with lock:
                return await registry_adapter.call(self.settings, tool_name, arguments, ctx)
        if transport in {"http", "sse", "stdio"}:
            if transport in {"http", "sse"} and not server.url:
                raise MCPError(f"server {server.name!r} has no url")
            key = self._session_key(server)

            deadline = asyncio.get_running_loop().time() + server.timeout_ms / 1000
            dispatched = False

            async def call(session):
                nonlocal dispatched
                async with asyncio.timeout_at(deadline):
                    dispatched = True
                    return await call_tool_on(session, tool_name, arguments)

            try:
                async with asyncio.timeout_at(deadline):
                    handle = await self._pooled_session(server)
                    return await handle.run(call)
            except asyncio.CancelledError:
                await self._pool.invalidate(key)
                raise
            except MCPError as exc:
                await self._pool.invalidate(key)
                if not dispatched:
                    raise MCPError(str(exc), stage="pre_send") from exc
                raise
            except Exception as exc:
                # A broken or timed-out session is not safe to reuse. Close it
                # so the next call reconnects; never replay the call itself.
                await self._pool.invalidate(key)
                raise MCPError(
                    str(exc) or type(exc).__name__,
                    stage="dispatch" if dispatched else "pre_send",
                ) from exc
        raise MCPError(f"unsupported transport {transport!r}")

    async def test_connection(self, server) -> dict[str, Any]:
        tools = await self.discover_tools(server)
        return {"ok": True, "tool_count": len(tools)}
