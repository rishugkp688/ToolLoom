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
from backend.services.task_service import TaskService
from backend.services.tool_errors import ToolPreflightError
from backend.utils.async_work import run_blocking
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
        self,
        db,
        settings,
        connection_manager: MCPConnectionManager,
        secret_resolver=None,
        registry=None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.connection_manager = connection_manager
        self.secret_resolver = secret_resolver
        # Handed to tools that change the catalogue themselves, such as
        # installing a generated capability.
        self.registry = registry

    def effect_of(self, namespaced_name: str) -> str:
        """The operator-controlled effect classification for a tool.

        Returns ``unknown`` for anything unrecognised, which keeps the caller on the
        safe path: no blind retry of an uncertain call, and no concurrency. A tool's
        own description is not consulted — an MCP server calling itself read-only is
        a statement of intent, not a guarantee.
        """
        with self.db.session() as session:
            row = ToolRepository(session).get_by_namespaced(namespaced_name)
            effect = getattr(row, "effect", None) if row is not None else None
        return effect or "unknown"

    def resolve_tool_name(self, name: str) -> str | None:
        """The immutable canonical identity for a requested tool name.

        Resolution must happen once, at the runtime boundary, so effect lookup,
        uncertainty matching, ledger rows, dispatch and UI events all use the same
        identity. A bare name and its namespaced form are otherwise two different
        keys for one operation.
        """
        if not name:
            return None
        return name if "__" in name else self._resolve_bare_name(name)

    def _resolve_bare_name(self, bare: str) -> str | None:
        """Map an unqualified tool name to its namespaced form when only one matches.

        The model reasons in tool *names*, not in `server__tool` identifiers, so it
        writes `shell`. When exactly one enabled tool on an enabled server has that
        suffix, honouring it is mechanical — and when two do, the ambiguity is
        reported instead of resolved arbitrarily.
        """
        if not bare:
            return None
        suffix = f"__{bare}"
        with self.db.session() as session:
            servers = MCPServerRepository(session)
            matches = []
            for tool in ToolRepository(session).list_enabled():
                if not tool.namespaced_name.endswith(suffix):
                    continue
                server = servers.get(tool.server_id)
                if server is not None and server.enabled:
                    matches.append(tool.namespaced_name)
        return matches[0] if len(matches) == 1 else None

    async def execute(self, call, conversation_id: str) -> ExecutionResult:
        started = time.perf_counter()
        prepared = await run_blocking(self._prepare, call, conversation_id, started)
        if isinstance(prepared, ExecutionResult):
            return prepared
        server, tool, context, server_name, tool_name, namespaced, transport = prepared
        attempts = 0
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
                # Retry only when the request provably never reached the server, and
                # only for a read-only tool. A connection error raised while a session
                # is being torn down can arrive after the tool already succeeded, so a
                # mutation must never be replayed automatically.
                effect = getattr(tool, "effect", "unknown") or "unknown"
                stage = getattr(exc, "stage", None)
                if stage is not None:
                    # The transport told us where it failed. Only a verified
                    # pre-send failure proves nothing reached the server.
                    pre_send = stage == "pre_send"
                else:
                    pre_send = transport == "local" and isinstance(exc, ToolPreflightError)
                    # Untyped connection failures can come from post-effect
                    # cleanup. Only a read-only call may use that legacy signal.
                    if effect == "read_only" and is_pre_send_error(exc):
                        pre_send = True
                if attempts < 2 and pre_send and effect == "read_only":
                    await asyncio.sleep(RETRY_BACKOFF_SECONDS)
                    continue
                detail = str(exc).strip() or "the tool reported no detail"
                result = to_result(
                    call,
                    await run_blocking(self._redact,
                        f"{namespaced} could not be executed — {type(exc).__name__}: {detail}"
                    ),
                    is_error=True,
                    attempts=attempts,
                    duration_ms=self._elapsed_ms(started),
                    max_chars=DEFAULT_MAX_CHARS,
                )
                # A timeout or dropped response does not prove the action failed.
                # Idempotent calls are safe to repeat, so they are not uncertain.
                result.outcome_uncertain = not pre_send and effect in {"mutating", "unknown"}
                return result

        result = to_result(
            call,
            await run_blocking(self._redact, outcome.text),
            is_error=outcome.is_error,
            attempts=attempts,
            duration_ms=self._elapsed_ms(started),
            max_chars=DEFAULT_MAX_CHARS,
        )
        result.outcome_uncertain = getattr(outcome, "outcome_uncertain", False) or (
            transport in {"stdio", "http", "sse", "generated"}
            and outcome.is_error
            and getattr(tool, "effect", "unknown") in {"unknown", "mutating"}
        )
        result.exit_code = getattr(outcome, "exit_code", None)
        result.verification_failed = getattr(outcome, "verification_failed", False)
        if result.truncated:
            await run_blocking(self._spill, conversation_id, result)
        return result

    def _prepare(self, call, conversation_id, started):
        namespaced = call.name or ""
        if "__" not in namespaced:
            # Small models often ask for `shell` when the tool is `sandbox__shell`.
            # Resolve an unqualified name only when it is unambiguous; a guess that
            # matches two servers is still refused.
            resolved = self._resolve_bare_name(namespaced)
            if resolved is None:
                return self._fail(call, f"invalid namespaced tool name {namespaced!r}", started)
            try:
                call.name = resolved
            except Exception:
                pass
            namespaced = resolved
        server_name, tool_name = namespaced.split("__", 1)
        transport = "http"

        with self.db.session() as session:
            server = MCPServerRepository(session).get_by_name(server_name)
            if server is None:
                return self._fail(call, f"unknown MCP server {server_name!r}", started)
            if not server.enabled:
                return self._fail(call, f"MCP server {server_name!r} is disabled", started)
            transport = getattr(server, "transport", "http")
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

        context = self._build_context(conversation_id)
        if namespaced in {"sandbox__skill_list", "sandbox__skill_read"} and not context.skills:
            return self._fail(
                call, "No visible skills are enabled; skill tools are unavailable.", started
            )
        return server, tool, context, server_name, tool_name, namespaced, transport

    # -- helpers --------------------------------------------------------------

    def _redact(self, text: str) -> str:
        if self.secret_resolver and hasattr(self.secret_resolver, "redact"):
            text = self.secret_resolver.redact(text)
        if self.settings.commandcode_api_key:
            text = text.replace(self.settings.commandcode_api_key, "[REDACTED]")
        return text

    def _redact_arguments(self, arguments: dict) -> dict:
        def redact(value):
            if isinstance(value, str):
                return self._redact(value)
            if isinstance(value, dict):
                return {self._redact(str(k)): redact(v) for k, v in value.items()}
            if isinstance(value, list):
                return [redact(item) for item in value]
            return value

        return redact(arguments)

    def _build_context(self, conversation_id: str) -> ToolContext:
        output_secrets = []
        if self.secret_resolver is not None and hasattr(self.secret_resolver, "redaction_values"):
            output_secrets.extend(self.secret_resolver.redaction_values())
        if self.settings.commandcode_api_key:
            output_secrets.append(self.settings.commandcode_api_key)
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
            registry=self.registry,
            tasks=TaskService(self.db, self.settings),
            output_secrets=tuple(output_secrets),
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
            from backend.utils.fs import atomic_write_text

            atomic_write_text(path, result.output_text)
        except Exception:
            return
        reference = f".transcripts/{safe_id}.txt"
        result.model_text = (
            f"[TRUNCATED — received tool result ({len(result.output_text)} chars) at "
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
