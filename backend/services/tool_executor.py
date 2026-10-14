"""Safe tool execution: authorization, schema validation, bounded dispatch.

Every failure mode returns a structured error result — the runtime never sees a
raised exception from here, and a tool is never claimed to have run without a
result.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from backend.repositories import MCPServerRepository, SkillRepository, ToolRepository
from backend.runtime.observation import DEFAULT_MAX_CHARS, ExecutionResult, to_result
from backend.services.mcp_connection_manager import MCPConnectionManager
from backend.services.sandbox import ToolContext, workspace_for
from backend.utils.errors import is_pre_send_error

MAX_VALIDATION_ERRORS = 10
RETRY_BACKOFF_SECONDS = 0.5


def safe_schema(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("invalid stored tool schema") from exc
    if not isinstance(parsed, dict):
        raise ValueError("invalid stored tool schema")
    return parsed


def validate_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> list[str]:
    if not schema:
        return []
    try:
        Draft202012Validator.check_schema(schema)

        def check_refs(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in {"$ref", "$dynamicRef"} and not str(child).startswith("#"):
                        raise ValueError("external schema references are not supported")
                    check_refs(child)
            elif isinstance(value, list):
                for child in value:
                    check_refs(child)

        check_refs(schema)
        validator = Draft202012Validator(schema)
    except (SchemaError, ValueError):
        return ["invalid or unsupported tool schema"]
    errors: list[str] = []
    for error in validator.iter_errors(arguments or {}):
        path = ".".join(str(part) for part in error.path) or "(root)"
        errors.append(f"{path}: {error.message}")
        if len(errors) >= MAX_VALIDATION_ERRORS:
            break
    return errors


class ToolExecutor:
    def __init__(
        self, db, settings, connection_manager: MCPConnectionManager, secret_resolver=None
    ) -> None:
        self.db = db
        self.settings = settings
        self.connection_manager = connection_manager
        self.secret_resolver = secret_resolver

    async def execute(self, call, conversation_id: str) -> ExecutionResult:
        started = time.perf_counter()
        namespaced = call.name or ""
        if "__" not in namespaced:
            return self._fail(call, f"invalid namespaced tool name {namespaced!r}", started)
        server_name, tool_name = namespaced.split("__", 1)

        with self.db.session() as session:
            server = MCPServerRepository(session).get_by_name(server_name)
            if server is None:
                return self._fail(call, f"unknown MCP server {server_name!r}", started)
            if not server.enabled:
                return self._fail(call, f"MCP server {server_name!r} is disabled", started)
            tool = ToolRepository(session).get_by_namespaced(namespaced)
            if tool is None:
                return self._fail(call, f"unknown tool {namespaced!r}", started)
            if not tool.enabled:
                return self._fail(call, f"tool {namespaced!r} is disabled", started)
            if tool.risk == "high" and not tool.approved:
                return self._fail(
                    call,
                    f"tool {namespaced!r} is high-risk and requires explicit operator approval",
                    started,
                )
            try:
                schema = safe_schema(tool.input_schema_json)
            except ValueError:
                return self._fail(call, "invalid stored tool schema", started)

        # Re-validate at execution time, not merely in the prompt.
        try:
            validation_errors = validate_arguments(schema, call.arguments or {})
        except Exception:
            validation_errors = ["invalid or unresolved tool schema"]
        if validation_errors:
            return self._fail(
                call,
                "invalid tool arguments -> " + "; ".join(validation_errors),
                started,
                validation_errors=validation_errors,
            )

        attempts = 0
        context = self._build_context(conversation_id)
        if namespaced in {"sandbox__skill_list", "sandbox__skill_read"} and not context.skills:
            return self._fail(
                call, "No visible skills are enabled; skill tools are unavailable.", started
            )
        while True:
            attempts += 1
            if attempts > 1:
                # A permission or connection may have changed during backoff.
                with self.db.session() as session:
                    current_server = MCPServerRepository(session).get_by_name(server_name)
                    current_tool = ToolRepository(session).get_by_namespaced(namespaced)
                    if (
                        current_server is None
                        or current_tool is None
                        or not current_server.enabled
                        or not current_tool.enabled
                        or (current_tool.risk == "high" and not current_tool.approved)
                        or current_tool.input_schema_json != tool.input_schema_json
                        or any(
                            getattr(current_server, key) != getattr(server, key)
                            for key in (
                                "url",
                                "headers_json",
                                "command",
                                "args_json",
                                "env_json",
                                "cwd",
                            )
                        )
                    ):
                        return self._fail(
                            call,
                            "tool permissions or connection changed before retry",
                            started,
                            attempts=attempts - 1,
                        )
            try:
                outcome = await self.connection_manager.call_tool(
                    server, tool_name, call.arguments or {}, context
                )
                break
            except Exception as exc:
                # Retry only when the request provably never reached the server.
                if attempts < 2 and is_pre_send_error(exc):
                    await asyncio.sleep(RETRY_BACKOFF_SECONDS)
                    continue
                return to_result(
                    call,
                    self._redact(f"{type(exc).__name__}: {exc}"),
                    is_error=True,
                    attempts=attempts,
                    duration_ms=self._elapsed_ms(started),
                    max_chars=DEFAULT_MAX_CHARS,
                )

        result = to_result(
            call,
            self._redact(outcome.text),
            is_error=outcome.is_error,
            attempts=attempts,
            duration_ms=self._elapsed_ms(started),
            max_chars=DEFAULT_MAX_CHARS,
        )
        if result.truncated:
            self._spill(conversation_id, result)
        return result

    # -- helpers --------------------------------------------------------------

    def _redact(self, text: str) -> str:
        if self.secret_resolver and hasattr(self.secret_resolver, "redact"):
            text = self.secret_resolver.redact(text)
        if self.settings.commandcode_api_key:
            text = text.replace(self.settings.commandcode_api_key, "[REDACTED]")
        return text

    def _build_context(self, conversation_id: str) -> ToolContext:
        skills: list[dict] = []
        try:
            with self.db.session() as session:
                for skill in SkillRepository(session).list_enabled():
                    if skill.hidden_from_model:
                        continue
                    skills.append(
                        {
                            "name": skill.name,
                            "description": skill.description,
                            "fs_path": skill.fs_path,
                        }
                    )
        except Exception:
            skills = []
        return ToolContext(
            conversation_id=conversation_id,
            workspaces_dir=self.settings.workspaces_dir,
            skills_root=self.settings.data_dir / "skills",
            skills=skills,
            secrets=self.secret_resolver,
            settings=self.settings,
        )

    def _spill(self, conversation_id: str, result: ExecutionResult) -> None:
        """Write the full tool output into the workspace and point the model at it."""
        try:
            workspace = workspace_for(self.settings.workspaces_dir, conversation_id)
            call_id = result.tool_call_id
            import re

            safe_id = (
                call_id
                if re.fullmatch(r"[A-Za-z0-9_-]{1,100}", call_id)
                else hashlib.sha256(call_id.encode()).hexdigest()
            )
            path = workspace.resolve(f".transcripts/{safe_id}.txt")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(result.output_text, encoding="utf-8")
        except Exception:
            return
        reference = f".transcripts/{safe_id}.txt"
        result.model_text = (
            f"[TRUNCATED — full output ({len(result.output_text)} chars) at "
            f"`{reference}`; use fs_read with offset/limit or fs_grep to retrieve "
            "any part.]\n\n" + result.model_text
        )

    def _fail(
        self,
        call,
        message: str,
        started: float,
        *,
        validation_errors: list[str] | None = None,
        attempts: int = 1,
    ) -> ExecutionResult:
        result = to_result(
            call,
            self._redact(message),
            is_error=True,
            attempts=attempts,
            duration_ms=self._elapsed_ms(started),
            max_chars=DEFAULT_MAX_CHARS,
        )
        if validation_errors:
            result.validation_errors = [self._redact(error) for error in validation_errors]
        return result

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return int((time.perf_counter() - started) * 1000)
