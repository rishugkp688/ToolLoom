from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import MCPServer, Tool
from backend.utils import utcnow

_UPDATABLE = {
    "name",
    "transport",
    "url",
    "headers_json",
    "enabled",
    "trusted",
    "verify_ssl",
    "timeout_ms",
    "command",
    "args_json",
    "cwd",
    "env_json",
}


class MCPServerRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, **fields) -> MCPServer:
        fields = {k: v for k, v in fields.items() if k in _UPDATABLE}
        if "headers_json" in fields and not isinstance(fields["headers_json"], str):
            fields["headers_json"] = json.dumps(fields["headers_json"])
        server = MCPServer(**fields)
        self.session.add(server)
        self.session.flush()
        return server

    def get(self, server_id: str) -> MCPServer | None:
        return self.session.get(MCPServer, server_id)

    def get_by_name(self, name: str) -> MCPServer | None:
        stmt = select(MCPServer).where(MCPServer.name == name)
        return self.session.execute(stmt).scalar_one_or_none()

    def list(self) -> list[MCPServer]:
        stmt = select(MCPServer).order_by(MCPServer.name)
        return list(self.session.execute(stmt).scalars().all())

    def update(self, server: MCPServer, **fields) -> MCPServer:
        for key, value in fields.items():
            if key not in _UPDATABLE or value is None:
                continue
            if key == "headers_json" and not isinstance(value, str):
                value = json.dumps(value)
            setattr(server, key, value)
        server.updated_at = utcnow()
        self.session.flush()
        return server

    def delete(self, server_id: str) -> bool:
        server = self.get(server_id)
        if server is None:
            return False
        self.session.delete(server)
        return True

    @staticmethod
    def headers_for(server: MCPServer) -> dict[str, str]:
        try:
            data = json.loads(server.headers_json or "{}")
        except json.JSONDecodeError:
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


class ToolRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_by_server(self, server_id: str) -> list[Tool]:
        stmt = select(Tool).where(Tool.server_id == server_id).order_by(Tool.namespaced_name)
        return list(self.session.execute(stmt).scalars().all())

    def list_enabled(self) -> list[Tool]:
        stmt = (
            select(Tool)
            .join(MCPServer)
            .where(
                Tool.enabled.is_(True),
                MCPServer.enabled.is_(True),
                (Tool.risk != "high") | Tool.approved.is_(True),
            )
            .order_by(Tool.namespaced_name)
        )
        return list(self.session.execute(stmt).scalars().all())

    def get_by_namespaced(self, namespaced_name: str) -> Tool | None:
        stmt = select(Tool).where(Tool.namespaced_name == namespaced_name)
        return self.session.execute(stmt).scalar_one_or_none()

    def set_enabled(self, namespaced_name: str, enabled: bool) -> Tool | None:
        tool = self.get_by_namespaced(namespaced_name)
        if tool is None:
            return None
        tool.enabled = enabled
        tool.updated_at = utcnow()
        self.session.flush()
        return tool

    def update(self, namespaced_name: str, **fields) -> Tool | None:
        tool = self.get_by_namespaced(namespaced_name)
        if tool is None:
            return None
        for key in ("enabled", "approved", "risk"):
            if key in fields and fields[key] is not None:
                if key == "risk" and fields[key] not in {"low", "medium", "high"}:
                    continue
                setattr(tool, key, fields[key])
        tool.updated_at = utcnow()
        self.session.flush()
        return tool

    def reconcile(self, server_id: str, discovered: list[dict], *, default_enabled: bool = False):
        """Add/update/remove tool rows to match a fresh discovery.

        Existing rows keep their ``enabled`` flag so operator toggles survive
        re-discovery. Newly discovered tools start with ``default_enabled``.
        Returns ``(added, updated, removed)`` counts.
        """
        existing = {t.name: t for t in self.list_by_server(server_id)}
        seen: set[str] = set()
        added = updated = removed = 0
        for item in discovered:
            name = item["name"]
            seen.add(name)
            namespaced = f"{item['server_name']}__{name}"
            schema_json = json.dumps(item.get("input_schema") or {})
            row = existing.get(name)
            if row is None:
                self.session.add(
                    Tool(
                        server_id=server_id,
                        name=name,
                        namespaced_name=namespaced,
                        description=item.get("description") or "",
                        input_schema_json=schema_json,
                        enabled=default_enabled,
                        risk=item.get("risk") or "low",
                    )
                )
                added += 1
            else:
                if json.loads(row.input_schema_json) != json.loads(schema_json):
                    row.enabled = False
                    row.approved = False
                if item.get("risk") == "high" and row.risk != "high":
                    row.enabled = False
                    row.approved = False
                changed = (
                    row.namespaced_name != namespaced
                    or row.description != (item.get("description") or "")
                    or row.input_schema_json != schema_json
                )
                row.namespaced_name = namespaced
                row.description = item.get("description") or ""
                row.input_schema_json = schema_json
                if item.get("risk"):
                    row.risk = item["risk"]
                if changed:
                    row.updated_at = utcnow()
                    updated += 1
        for name, row in existing.items():
            if name not in seen:
                self.session.delete(row)
                removed += 1
        self.session.flush()
        return added, updated, removed
