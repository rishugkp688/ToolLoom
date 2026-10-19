"""Persisted MCP server/tool catalogue: discovery, namespacing, enable flags."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from threading import RLock
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from backend.repositories import MCPServerRepository, ToolRepository
from backend.schemas.requests import CreateMCPServerRequest
from backend.services.mcp_connection_manager import MCPConnectionManager
from backend.services.sandbox import available_local_servers, server_names
from backend.services.tool_foundry.registry_adapter import GENERATED_SERVER, describe, discover
from backend.utils.jsonc import loads as jsonc_loads

# Servers the runtime owns. They cannot be created, renamed or removed through
# the operator API, only enabled and disabled.
_IN_PROCESS_TRANSPORTS = frozenset({"local", "generated"})


def reserved_server_names() -> set[str]:
    return set(server_names()) | {GENERATED_SERVER}

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_STORED = "<stored; unchanged>"
_SAFE_REFERENCE = re.compile(r"(?:Bearer )?\$\{secrets\.[A-Za-z0-9_]{1,64}\}")


class RegistryError(ValueError):
    """Invalid registry input (bad name, unsupported transport, bad config)."""


@dataclass
class DiscoveryResult:
    server_id: str
    added: int
    updated: int
    removed: int
    tools: list[Any] = field(default_factory=list)


def validate_server_name(name: str) -> str:
    if not _NAME_RE.match(name or ""):
        raise RegistryError("server name must be 1-64 chars of [A-Za-z0-9_-]")
    if "__" in name:
        raise RegistryError("server name may not contain '__' (reserved for namespacing)")
    return name


class ToolRegistry:
    def __init__(self, db, settings, connection_manager: MCPConnectionManager) -> None:
        self.db = db
        self.settings = settings
        self.connection_manager = connection_manager
        self._writes = RLock()

    # -- seeding --------------------------------------------------------------

    def seed(self) -> None:
        """Ensure the shipped built-in local servers exist, then reconcile tools.

        This is a floor, not a one-time seed: if the operator deletes a built-in
        row it is recreated on the next boot. Enablement set by the operator is
        preserved, and individual tools stay disabled until explicitly enabled.

        The `demo` fixture used by the test and load harnesses is not shipped; it
        is seeded only when ``TOOLLOOM_SEED_DEMO`` is set.
        """
        names = list(server_names())
        if self.settings.toolloom_seed_demo and "demo" in available_local_servers():
            names.append("demo")
        with self.db.session() as session:
            servers = MCPServerRepository(session)
            tools = ToolRepository(session)
            for name in names:
                server = servers.get_by_name(name)
                if server is None:
                    server = servers.create(
                        name=name,
                        transport="local",
                        url=f"local://{name}",
                        enabled=True,
                        trusted=True,
                        timeout_ms=60_000,
                    )
                else:
                    server.transport = "local"
                    server.url = f"local://{name}"
                discovered = self.connection_manager.local_tools(server)
                tools.reconcile(server.id, discovered, default_enabled=False)
                if name == "sandbox":
                    fetch_tool = tools.get_by_namespaced("sandbox__http_fetch")
                    if fetch_tool is not None:
                        # Upgrade historical installs: the fixed public contract
                        # permits POST/PATCH/DELETE and cannot promise idempotency.
                        fetch_tool.effect = "mutating"
        self.sync_generated()

    def sync_generated(self) -> tuple[int, int, int]:
        """Reconcile the `generated` server's tools with what is installed.

        Called at boot and after an install or a removal, so the catalogue
        follows the store. New tools arrive disabled; an existing tool keeps its
        enable and approval flags unless its schema changed, in which case the
        repository revokes the approval.
        """
        with self._writes, self.db.session() as session:
            servers = MCPServerRepository(session)
            tools = ToolRepository(session)
            server = servers.get_by_name(GENERATED_SERVER)
            if server is None:
                server = servers.create(
                    name=GENERATED_SERVER,
                    transport="generated",
                    enabled=True,
                    trusted=True,
                    timeout_ms=60_000,
                )
            else:
                server.transport = "generated"
            return tools.reconcile(server.id, discover(self.settings), default_enabled=False)

    def generated_capabilities(self) -> list[dict]:
        """Installed capabilities with the evidence the review view needs."""
        capabilities = describe(self.settings)
        with self.db.session() as session:
            tools = ToolRepository(session)
            for entry in capabilities:
                row = tools.get_by_namespaced(entry["tool"])
                entry["enabled"] = bool(row.enabled) if row else False
                entry["approved"] = bool(row.approved) if row else False
                entry["risk"] = row.risk if row else "high"
                entry["tool_id"] = row.id if row else None
        return capabilities

    # -- servers --------------------------------------------------------------

    def list_servers(self) -> list[Any]:
        with self.db.session() as session:
            return MCPServerRepository(session).list()

    def get_server(self, server_id: str):
        with self.db.session() as session:
            return MCPServerRepository(session).get(server_id)

    def get_server_by_name(self, name: str):
        with self.db.session() as session:
            return MCPServerRepository(session).get_by_name(name)

    def create_server(
        self,
        *,
        name: str,
        transport: str = "http",
        url: str | None = None,
        headers: dict[str, str] | None = None,
        enabled: bool = False,
        trusted: bool = False,
        verify_ssl: bool = True,
        timeout_ms: int = 30_000,
        command: str | None = None,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ):
        fields = self._validate(
            {
                "name": name,
                "transport": transport,
                "url": url,
                "headers": headers or {},
                "enabled": enabled,
                "trusted": trusted,
                "verify_ssl": verify_ssl,
                "timeout_ms": timeout_ms,
                "command": command,
                "args": args or [],
                "cwd": cwd,
                "env": env or {},
            }
        )
        with self._writes, self.db.session() as session:
            repo = MCPServerRepository(session)
            if repo.get_by_name(name) is not None:
                raise RegistryError(f"server {name!r} already exists")
            try:
                return repo.create(**fields)
            except IntegrityError as exc:
                raise RegistryError("server name already exists") from exc

    def _validate(self, values: dict) -> dict:
        try:
            body = CreateMCPServerRequest.model_validate(values, strict=True)
        except ValidationError as exc:
            # Pydantic's default message includes input values (possibly secrets).
            fields = ", ".join(".".join(map(str, e["loc"])) for e in exc.errors())
            raise RegistryError(f"invalid config fields: {fields}") from exc
        validate_server_name(body.name)
        if body.name in reserved_server_names() or body.transport in _IN_PROCESS_TRANSPORTS:
            raise RegistryError(
                "runtime-owned server names and in-process transports are reserved"
            )
        if not body.verify_ssl:
            raise RegistryError("TLS verification cannot be disabled")
        if body.transport == "stdio":
            if not self.settings.toolloom_allow_stdio:
                raise RegistryError("stdio requires TOOLLOOM_ALLOW_STDIO=1")
            if not body.command or not body.command.strip() or body.url or body.headers:
                raise RegistryError("stdio requires a command and does not accept url/headers")
        else:
            try:
                parsed = urlsplit(body.url or "")
                valid = parsed.scheme in {"http", "https"} and parsed.hostname and parsed.port != 0
            except ValueError as exc:
                raise RegistryError("invalid server URL") from exc
            if not valid or parsed.username or parsed.password or parsed.fragment:
                raise RegistryError("http/sse requires an http(s) URL without embedded credentials")
            if body.command or body.args or body.cwd or body.env:
                raise RegistryError("command/args/cwd/env are only supported for stdio")
        if any("\n" in k + v or "\r" in k + v for k, v in body.headers.items()):
            raise RegistryError("header names and values may not contain newlines")
        if any(not k or "=" in k or "\x00" in k + v for k, v in body.env.items()):
            raise RegistryError("invalid environment entry")
        values = body.model_dump()
        values["headers_json"] = values.pop("headers")
        values["args_json"] = json.dumps(values.pop("args") or [])
        values["env_json"] = json.dumps(values.pop("env"))
        return values

    @staticmethod
    def _values(server) -> dict:
        return {
            "name": server.name,
            "transport": server.transport,
            "url": server.url,
            "headers": json.loads(server.headers_json or "{}"),
            "enabled": server.enabled,
            "trusted": server.trusted,
            "verify_ssl": server.verify_ssl,
            "timeout_ms": server.timeout_ms,
            "command": server.command,
            "args": json.loads(server.args_json or "[]"),
            "cwd": server.cwd,
            "env": json.loads(server.env_json or "{}"),
        }

    @staticmethod
    def _revoke(session, server_id):
        for tool in ToolRepository(session).list_by_server(server_id):
            tool.enabled = False
            tool.approved = False

    def update_server(self, server_id: str, **fields):
        with self._writes, self.db.session() as session:
            repo = MCPServerRepository(session)
            server = repo.get(server_id)
            if server is None:
                return None
            if server.transport in _IN_PROCESS_TRANSPORTS:
                if set(fields) - {"enabled"}:
                    raise RegistryError(
                        "in-process (local) servers support enable/disable only"
                    )
            else:
                values = self._values(server)
                values.update(
                    {("headers" if k == "headers_json" else k): v for k, v in fields.items()}
                )
                self._validate(values)
                if any(k != "enabled" and v != getattr(server, k) for k, v in fields.items()):
                    self._revoke(session, server_id)
                    self.connection_manager.invalidate_server_soon(server_id)
            return repo.update(server, **fields)

    def delete_server(self, server_id: str) -> bool:
        with self._writes, self.db.session() as session:
            repo = MCPServerRepository(session)
            server = repo.get(server_id)
            if server and server.name in reserved_server_names():
                raise RegistryError("runtime-owned servers can be disabled, not removed")
            deleted = repo.delete(server_id)
            if deleted:
                self.connection_manager.invalidate_server_soon(server_id)
            return deleted

    # -- tools ----------------------------------------------------------------

    def list_tools(self, server_id: str) -> list[Any]:
        with self.db.session() as session:
            return ToolRepository(session).list_by_server(server_id)

    def tool_state(self, namespaced_name: str) -> dict[str, Any] | None:
        """The current enable/approval/risk state, after any reconciliation."""
        with self.db.session() as session:
            row = ToolRepository(session).get_by_namespaced(namespaced_name)
            if row is None:
                return None
            return {
                "enabled": bool(row.enabled),
                "approved": bool(row.approved),
                "risk": row.risk,
            }

    def set_tool_enabled(self, server_id: str, tool_name: str, enabled: bool):
        return self.update_tool(server_id, tool_name, enabled=enabled)

    def update_tool(self, server_id: str, tool_name: str, **fields):
        with self._writes, self.db.session() as session:
            server = MCPServerRepository(session).get(server_id)
            if server is None:
                return None
            namespaced = f"{server.name}__{tool_name}"
            if namespaced in {"sandbox__python", "sandbox__shell", "sandbox__http_fetch"}:
                if fields.get("risk", "high") != "high":
                    raise RegistryError("this built-in tool must remain high-risk")
            if namespaced == "sandbox__python" and fields.get("enabled"):
                if not self.settings.toolloom_allow_python:
                    raise RegistryError("Python requires TOOLLOOM_ALLOW_PYTHON=1 and a restart")
            if namespaced == "sandbox__shell" and fields.get("enabled"):
                if not self.settings.toolloom_allow_shell:
                    raise RegistryError(
                        "Shell execution requires TOOLLOOM_ALLOW_SHELL=1 and a restart"
                    )
            return ToolRepository(session).update(namespaced, **fields)

    async def discover(self, server_id: str) -> DiscoveryResult:
        server = self.get_server(server_id)
        if server is None:
            raise RegistryError("server not found")
        discovered = await self.connection_manager.discover_tools(server)
        with self._writes, self.db.session() as session:
            current = MCPServerRepository(session).get(server_id)
            if current is None or self._values(current) != self._values(server):
                raise RegistryError("server changed during discovery; retry")
            added, updated, removed = ToolRepository(session).reconcile(
                server.id, discovered, default_enabled=False
            )
            tools = ToolRepository(session).list_by_server(server.id)
            return DiscoveryResult(server_id, added, updated, removed, list(tools))

    async def test_connection(self, server_id: str) -> dict[str, Any]:
        server = self.get_server(server_id)
        if server is None:
            raise RegistryError("server not found")
        return await self.connection_manager.test_connection(server)

    # -- import ---------------------------------------------------------------

    def export_config(self) -> dict:
        """Editable snapshot of SQLite state; never resolve or expose credentials."""
        servers = {}
        redacted = False
        for server in self.list_servers():
            if server.transport in _IN_PROCESS_TRANSPORTS:
                continue  # not an operator-editable connection
            values = self._values(server)
            spec = {"type": values["transport"], "timeout_ms": values["timeout_ms"]}
            if server.transport == "stdio":
                spec.update(command=server.command, args=values["args"])
                if server.cwd:
                    spec["cwd"] = server.cwd
                mapping = values["env"]
                key = "env"
            else:
                # Query strings can contain credentials; keep their value server-side.
                spec["url"] = _STORED if urlsplit(server.url).query else server.url
                redacted |= spec["url"] == _STORED
                mapping = values["headers"]
                key = "headers"
            if mapping:
                spec[key] = {
                    k: v if _SAFE_REFERENCE.fullmatch(v) else _STORED for k, v in mapping.items()
                }
                redacted |= _STORED in spec[key].values()
            servers[server.name] = spec
        return {"config": {"mcpServers": servers}, "redacted": redacted}

    def import_config(self, text: str, *, sync: bool = False) -> dict[str, Any]:
        data = jsonc_loads(text)
        if (
            not isinstance(data, dict)
            or len(data) != 1
            or not (set(data) & {"servers", "mcpServers"})
        ):
            raise RegistryError("config must contain only a 'servers' or 'mcpServers' object")
        servers = next(iter(data.values()))
        if not isinstance(servers, dict) or len(servers) > 100:
            raise RegistryError("servers must be an object with at most 100 entries")
        results = []
        prepared = []
        with self._writes, self.db.session() as session:
            repo = MCPServerRepository(session)
            for name, spec in servers.items():
                try:
                    validate_server_name(name)
                    if not isinstance(spec, dict):
                        raise RegistryError("invalid server spec")
                    spec = dict(spec)
                    existing = repo.get_by_name(name)
                    old = self._values(existing) if existing else {}
                    for key in ("headers", "env"):
                        if isinstance(spec.get(key), dict):
                            spec[key] = dict(spec[key])
                            for k, v in spec[key].items():
                                if v == _STORED:
                                    if k not in old.get(key, {}):
                                        raise RegistryError(
                                            "stored credential placeholder has no existing value"
                                        )
                                    spec[key][k] = old[key][k]
                    if spec.get("url") == _STORED:
                        if not old.get("url"):
                            raise RegistryError("stored URL placeholder has no existing value")
                        spec["url"] = old["url"]
                    if "type" in spec and "transport" in spec:
                        raise RegistryError("use type or transport, not both")
                    declared = spec.pop("type", spec.pop("transport", None))
                    transport = declared or (
                        "stdio"
                        if "command" in spec
                        else "sse"
                        if str(spec.get("url", "")).rstrip("/").endswith("/sse")
                        else "http"
                    )
                    if transport in {"streamable-http", "streamable_http"}:
                        transport = "http"
                    fields = self._validate({**spec, "name": name, "transport": transport})
                    prepared.append((name, existing, fields))
                    results.append({"name": name, "ok": True, "transport": transport})
                except (RegistryError, TypeError, ValueError) as exc:
                    results.append({"name": name, "ok": False, "error": str(exc)})
            if any(not result["ok"] for result in results):
                return {"results": results, "applied": False, "removed": 0}
            for (_name, existing, fields), result in zip(prepared, results, strict=True):
                # Import never grants permissions, even if enabled appears in a file.
                fields.pop("enabled", None)
                fields.pop("trusted", None)
                if existing is None:
                    server = repo.create(**fields, enabled=False)
                else:
                    changed = any(
                        getattr(existing, k) != (json.dumps(v) if k == "headers_json" else v)
                        for k, v in fields.items()
                    )
                    if changed:
                        self._revoke(session, existing.id)
                        existing.enabled = False
                        self.connection_manager.invalidate_server_soon(existing.id)
                    server = repo.update(existing, **fields)
                    # Repository patch ignores None; import must clear removed fields.
                    for k in ("url", "command", "cwd"):
                        setattr(server, k, fields[k])
                result["id"] = server.id
            removed = 0
            if sync:
                for server in repo.list():
                    if (
                        server.name not in servers
                        and server.transport not in _IN_PROCESS_TRANSPORTS
                    ):
                        repo.delete(server.id)
                        self.connection_manager.invalidate_server_soon(server.id)
                        removed += 1
        return {"results": results, "applied": True, "removed": removed}
