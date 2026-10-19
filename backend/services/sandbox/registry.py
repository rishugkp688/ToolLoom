"""In-process local tool servers.

These present the same capability model as external MCP servers — discovered via
the registry, namespaced ``<server>__<tool>``, schema-validated, dispatched
through the executor — but open no socket.

Two built-in servers:

* ``demo``    — the deterministic ``add`` fixture.
* ``sandbox`` — workspace filesystem tools plus skill discovery/read.

Python is registered high-risk and dispatched asynchronously behind an explicit
operator opt-in. It is not OS-isolated (BUILD_SPEC §7).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from backend.services.sandbox import foundry_tools, fs_tool, http_tool, search_tool
from backend.services.sandbox.workspace import Workspace, workspace_for
from backend.services.skills import read_skill_body
from backend.services.task_service import PLAN_STATUSES, TASK_STATUSES
from backend.services.tool_errors import ToolPreflightError
from backend.services.tool_foundry import authoring
from backend.services.tool_foundry.manifest import FoundryError

MAX_PLAN_STEPS = 50


class SandboxError(RuntimeError):
    """Raised by a local tool handler. Surfaces as a structured tool error."""


@dataclass
class ToolContext:
    conversation_id: str
    workspaces_dir: Path
    skills_root: Path | None = None
    skills: list[dict[str, Any]] = field(default_factory=list)
    secrets: Any = None
    # Read-only view of the operator's Settings for tools that need limits or
    # provider choices. Never contains credentials.
    settings: Any = None
    # The tool registry, for the rare tool that changes the catalogue: installing
    # a generated capability must make it visible without a restart.
    registry: Any = None
    # Durable task state (objective, plan, completion criteria). The plan tools read
    # and revise it; the runtime pins it into the system prompt.
    tasks: Any = None
    # Backend-only data for redacting captures before they reach disk.
    output_secrets: tuple[str, ...] = ()

    def workspace(self) -> Workspace:
        return workspace_for(self.workspaces_dir, self.conversation_id)


Handler = Callable[[dict[str, Any], ToolContext], str]


@dataclass(frozen=True)
class SandboxTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler
    risk: str = "low"


# How our own tools affect the world. This is our classification, not a tool's
# self-description: anything absent stays `unknown`, which keeps it sequential and
# blocks blind retries after an uncertain outcome.
EFFECTS: dict[str, str] = {
    "sandbox__fs_read": "read_only",
    "sandbox__fs_list": "read_only",
    "sandbox__fs_grep": "read_only",
    "sandbox__skill_list": "read_only",
    "sandbox__skill_read": "read_only",
    "sandbox__plan_read": "read_only",
    "sandbox__plan_write": "idempotent",
    "sandbox__fs_write": "mutating",
    "sandbox__fs_edit": "mutating",
    "sandbox__repo_instructions": "read_only",
    "sandbox__fs_delete": "mutating",
    # The public contract includes POST/PATCH/DELETE; interrupted effects are unknown.
    "sandbox__http_fetch": "mutating",
    "sandbox__web_search": "idempotent",
    "demo__add": "read_only",
    # python, shell and the foundry tools stay unknown: they execute arbitrary code,
    # so repeating one after an uncertain outcome is exactly what must not happen.
}


# --- demo --------------------------------------------------------------------


def _demo_add(arguments: dict[str, Any], ctx: ToolContext) -> str:
    a = arguments.get("a")
    b = arguments.get("b")
    if isinstance(a, bool) or isinstance(b, bool):
        raise SandboxError("`a` and `b` must be integers")
    if not isinstance(a, int) or not isinstance(b, int):
        raise SandboxError("`a` and `b` must be integers")
    return str(a + b)


# --- filesystem --------------------------------------------------------------


def _fs_read(arguments: dict[str, Any], ctx: ToolContext) -> str:
    return fs_tool.read(ctx.workspace(), arguments)


def _fs_write(arguments: dict[str, Any], ctx: ToolContext) -> str:
    return fs_tool.write(ctx.workspace(), arguments)


def _fs_edit(arguments: dict[str, Any], ctx: ToolContext) -> str:
    return fs_tool.edit(ctx.workspace(), arguments)


def _repo_instructions(arguments: dict[str, Any], ctx: ToolContext) -> str:
    from backend.services.repository_instructions import discover

    return json.dumps({"files": discover(ctx.workspace(), arguments.get("path") or ".")})


def _fs_list(arguments: dict[str, Any], ctx: ToolContext) -> str:
    return fs_tool.list_dir(ctx.workspace(), arguments)


def _fs_grep(arguments: dict[str, Any], ctx: ToolContext) -> str:
    return fs_tool.grep(ctx.workspace(), arguments)


def _fs_delete(arguments: dict[str, Any], ctx: ToolContext) -> str:
    return fs_tool.delete(ctx.workspace(), arguments)


def _http_fetch(arguments: dict[str, Any], ctx: ToolContext) -> str:
    return http_tool.fetch(arguments, ctx)


def _web_search(arguments: dict[str, Any], ctx: ToolContext) -> str:
    return search_tool.search(arguments, ctx)


# --- task state --------------------------------------------------------------


def _plan_read(arguments: dict[str, Any], ctx: ToolContext) -> str:
    if ctx.tasks is None:
        raise SandboxError("task state is unavailable in this build")
    task = ctx.tasks.active(ctx.conversation_id)
    if task is None:
        return json.dumps({"objective": None, "plan": [], "plan_revision": 0}, indent=2)
    return json.dumps(
        {
            "original_request": task.original_request,
            "objective": task.objective,
            "objective_revision": task.objective_revision,
            "completion_criteria": task.completion_criteria,
            "plan": ctx.tasks.plan_of(task),
            "plan_revision": task.plan_revision,
        },
        indent=2,
        sort_keys=True,
    )


def _plan_write(arguments: dict[str, Any], ctx: ToolContext) -> str:
    if ctx.tasks is None:
        raise ToolPreflightError("task state is unavailable in this build")
    objective = arguments.get("objective")
    criteria = arguments.get("completion_criteria")
    raw_plan = arguments.get("plan")
    status = arguments.get("status")
    if objective is None and criteria is None and raw_plan is None and status is None:
        raise ToolPreflightError(
            "provide at least one of `plan`, `objective`, `completion_criteria`, `status`"
        )
    if status is not None and status not in TASK_STATUSES:
        raise ToolPreflightError(f"status must be one of {', '.join(TASK_STATUSES)}")

    plan = None
    if raw_plan is not None:
        if not isinstance(raw_plan, list) or len(raw_plan) > MAX_PLAN_STEPS:
            raise ToolPreflightError(f"plan must be a list of at most {MAX_PLAN_STEPS} steps")
        plan = []
        for index, item in enumerate(raw_plan, start=1):
            if not isinstance(item, dict):
                raise ToolPreflightError(f"plan step {index} must be an object")
            step = str(item.get("step") or "").strip()
            step_status = str(item.get("status") or "pending")
            if not step:
                raise ToolPreflightError(f"plan step {index} needs a non-empty `step`")
            if step_status not in PLAN_STATUSES:
                raise ToolPreflightError(
                    f"plan step {index} status must be one of {', '.join(PLAN_STATUSES)}"
                )
            plan.append({"step": step[:500], "status": step_status})

    task = ctx.tasks.revise(
        ctx.conversation_id,
        objective=objective,
        plan=plan,
        completion_criteria=criteria,
    )
    if status is not None:
        updated = ctx.tasks.set_status(ctx.conversation_id, status)
        if updated is not None:
            task = updated
    ctx.tasks.project_plan(ctx.workspace(), ctx.conversation_id)
    return json.dumps(
        {
            "status": task.status,
            "objective_revision": task.objective_revision,
            "plan_revision": task.plan_revision,
            "plan": ctx.tasks.plan_of(task),
        },
        indent=2,
        sort_keys=True,
    )


# --- tool foundry ------------------------------------------------------------

def _foundry_scaffold(arguments: dict[str, Any], ctx: ToolContext) -> str:
    if not getattr(ctx.settings, "toolloom_allow_foundry", False):
        raise SandboxError(foundry_tools.DISABLED_MESSAGE)
    name = str(arguments.get("name") or "").strip()
    description = str(arguments.get("description") or "").strip()
    schema = arguments.get("input_schema")
    if not isinstance(schema, dict):
        raise SandboxError("`input_schema` must be a JSON object")
    try:
        result = authoring.scaffold_package(ctx.workspace(), name, description, schema)
    except (FoundryError, ValidationError) as exc:
        raise SandboxError(str(exc)) from exc
    return json.dumps(result, indent=2, sort_keys=True)


def _foundry_async_only(arguments: dict[str, Any], ctx: ToolContext) -> str:
    raise SandboxError("Tool Foundry validation and installation need the async dispatcher")


def _python_async_only(arguments: dict[str, Any], ctx: ToolContext) -> str:
    raise SandboxError("Python requires the async execution dispatcher")


def _shell_async_only(arguments: dict[str, Any], ctx: ToolContext) -> str:
    raise SandboxError("Shell execution requires the async dispatcher")


# --- skills ------------------------------------------------------------------


def _skill_list(arguments: dict[str, Any], ctx: ToolContext) -> str:
    if not ctx.skills:
        return "(no skills enabled)"
    lines = [f"{s['name']}: {s['description']}" for s in ctx.skills]
    return "\n".join(lines)


def _skill_read(arguments: dict[str, Any], ctx: ToolContext) -> str:
    name = str(arguments.get("name") or "").strip()
    match = next((s for s in ctx.skills if s["name"] == name), None)
    if match is None:
        raise SandboxError(f"unknown or disabled skill {name!r}")
    return read_skill_body(match["fs_path"], arguments.get("file"))


_PATH = {"type": "string", "description": "Workspace-relative path"}

LOCAL_SERVERS: dict[str, list[SandboxTool]] = {
    "demo": [
        SandboxTool(
            name="add",
            description=(
                "Add two integers and return their sum. Deterministic; used to prove "
                "the tool-call round trip."
            ),
            input_schema={
                "type": "object",
                "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                "required": ["a", "b"],
                "additionalProperties": False,
            },
            handler=_demo_add,
        )
    ],
    "sandbox": [
        SandboxTool(
            name="plan_read",
            description=(
                "Read the current task objective and plan. Check it before changing "
                "direction so you do not overwrite newer progress."
            ),
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=_plan_read,
        ),
        SandboxTool(
            name="plan_write",
            description=(
                "Replace the task plan, objective or completion criteria. The plan is "
                "pinned outside summarisation, so it survives however long the run gets. "
                "Keep it current as you work: mark steps in_progress or completed, and add "
                "steps you discover. The original request is immutable and is never "
                "overwritten; each change bumps that field's revision."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "objective": {"type": "string", "maxLength": 2000},
                    "plan": {
                        "type": "array",
                        "maxItems": MAX_PLAN_STEPS,
                        "items": {
                            "type": "object",
                            "properties": {
                                "step": {"type": "string", "minLength": 1, "maxLength": 500},
                                "status": {
                                    "type": "string",
                                    "enum": list(PLAN_STATUSES),
                                },
                            },
                            "required": ["step"],
                            "additionalProperties": False,
                        },
                    },
                    "completion_criteria": {"type": "string", "maxLength": 1000},
                    "status": {
                        "type": "string",
                        "enum": list(TASK_STATUSES),
                        "description": (
                            "Mark the task active or completed. Completing it records "
                            "that the objective as written has been met."
                        ),
                    },
                },
                "additionalProperties": False,
            },
            handler=_plan_write,
        ),
        SandboxTool(
            name="python",
            description=(
                "Run Python in the conversation workspace. Executes with host OS "
                "permissions, NOT sandboxed. Requires operator opt-in and approval. "
                "Use print() for output; standard library and installed packages are available. "
                "Timeout is capped by operator settings; the result reports its effective value. "
                "Read output_captures paths with fs_read to retrieve omitted output."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "minLength": 1, "maxLength": 32000},
                    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120},
                },
                "required": ["code"],
                "additionalProperties": False,
            },
            handler=_python_async_only,
            risk="high",
        ),
        SandboxTool(
            name="shell",
            description=(
                "Run a shell command and read its output. This is the verification "
                "tool: prefer it for running the test suite, a build, a linter or "
                "a formatter check after you change files, so you can see whether "
                "your work actually passes. Runs through a real shell with host OS "
                "permissions, NOT sandboxed, so use the narrowest command that "
                "answers the question and pass non-interactive flags. A pipeline "
                "under Bash uses pipefail to retain upstream failures; exit 141 may "
                "mean an early-closed writer. The result reports shell, pipeline policy "
                "and the operator-capped effective timeout. Foreground descendants are "
                "stopped; '&' does not create a persistent background job. Read "
                "output_captures paths with fs_read for omitted output. The default "
                "working directory is the conversation workspace. Requires opt-in and approval."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "minLength": 1, "maxLength": 8000},
                    "cwd": {
                        "type": "string",
                        "description": "Working directory; defaults to the workspace",
                    },
                    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            handler=_shell_async_only,
            risk="high",
        ),
        SandboxTool(
            name="fs_read",
            description=("Read workspace text, including output capture paths. Paged output is "
                         "bounded; use offset/column from a truncation footer to continue."),
            input_schema={
                "type": "object",
                "properties": {
                    "path": _PATH,
                    "offset": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "1-based first line",
                    },
                    "limit": {"type": "integer", "minimum": 1, "description": "number of lines"},
                    "column": {"type": "integer", "minimum": 1,
                               "description": "1-based character in the first requested line"},
                    "with_metadata": {
                        "type": "boolean",
                        "description": "Full-file read returns JSON with sha256 for fs_edit",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            handler=_fs_read,
        ),
        SandboxTool(
            name="fs_write",
            description=(
                "Create or append to a text file in the workspace. A write replaces "
                "the file atomically. Pass `expected_sha256` from a prior read/write "
                "to refuse an overwrite after the file changed underneath you."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": _PATH,
                    "content": {"type": "string"},
                    "mode": {"type": "string", "enum": ["write", "append"]},
                    "expected_sha256": {
                        "type": "string",
                        "pattern": "^[0-9a-fA-F]{64}$",
                        "description": "Optional revision check from a prior read/write",
                    },
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            handler=_fs_write,
        ),
        SandboxTool(
            name="fs_edit",
            description=(
                "Replace one unique exact text fragment in an existing UTF-8 workspace file. "
                "Read with fs_read(with_metadata=true) first and provide its sha256. "
                "Refuses stale revisions and ambiguous matches; preserves other content."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": _PATH,
                    "old_text": {"type": "string", "minLength": 1},
                    "new_text": {"type": "string"},
                    "expected_sha256": {"type": "string", "pattern": "^[0-9a-fA-F]{64}$"},
                },
                "required": ["path", "old_text", "new_text", "expected_sha256"],
                "additionalProperties": False,
            },
            handler=_fs_edit,
        ),
        SandboxTool(
            name="repo_instructions",
            description=(
                "Discover AGENTS.md and CLAUDE.md from workspace root to a target file's "
                "directory, in that order. Read before editing a nested directory; deeper "
                "instructions apply only within that directory. Results are bounded."
            ),
            input_schema={
                "type": "object", "properties": {"path": _PATH},
                "additionalProperties": False,
            },
            handler=_repo_instructions,
        ),
        SandboxTool(
            name="fs_list",
            description="List files and directories in the workspace.",
            input_schema={
                "type": "object",
                "properties": {"path": _PATH},
                "additionalProperties": False,
            },
            handler=_fs_list,
        ),
        SandboxTool(
            name="fs_grep",
            description="Search workspace files with a regular expression.",
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": _PATH,
                    "ignore_case": {"type": "boolean"},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
            handler=_fs_grep,
        ),
        SandboxTool(
            name="fs_delete",
            description="Delete a file or directory from the workspace.",
            input_schema={
                "type": "object",
                "properties": {"path": _PATH, "recursive": {"type": "boolean"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            handler=_fs_delete,
        ),
        SandboxTool(
            name="skill_list",
            description="List the skills currently enabled for this agent.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=_skill_list,
        ),
        SandboxTool(
            name="skill_read",
            description="Read an enabled skill's SKILL.md (or a file within it).",
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}, "file": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False,
            },
            handler=_skill_read,
        ),
        SandboxTool(
            name="http_fetch",
            description=(
                "Fetch a public http(s) URL. Private/loopback hosts are refused; "
                "connections are pinned with TLS hostname checks. Cross-origin redirects "
                "with caller headers or mutations are refused. Mutating methods may have "
                "effects even if a response fails. High-risk; requires operator approval."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "method": {"type": "string"},
                    "headers": {"type": "object"},
                    "body": {"type": "string"},
                },
                "required": ["url"],
                "additionalProperties": False,
            },
            handler=_http_fetch,
            risk="high",
        ),
        SandboxTool(
            name="web_search",
            description=(
                "Search the public web and return ranked titles, URLs and snippets. "
                "Sends only the query text to the configured provider (no credentials, "
                "files or conversation history). High-risk egress: an operator must "
                "enable and approve it. Follow up with http_fetch or fs_write to read a "
                "result in full."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 400},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=_web_search,
            risk="high",
        ),
        SandboxTool(
            name="foundry_scaffold",
            description=(
                "Start a reusable capability from a script you have been running. Writes a "
                "package under foundry/<name>/ with manifest.json, a CLI skeleton whose flags "
                "match the schema you pass, and a test file. Fill in the logic, then call "
                "foundry_validate, then foundry_install. Requires TOOLLOOM_ALLOW_FOUNDRY=1."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "pattern": "^[a-z][a-z0-9_]{1,47}$"},
                    "description": {"type": "string", "maxLength": 500},
                    "input_schema": {
                        "type": "object",
                        "description": "JSON Schema for the tool's arguments, type object",
                    },
                },
                "required": ["name", "description", "input_schema"],
                "additionalProperties": False,
            },
            handler=_foundry_scaffold,
        ),
        SandboxTool(
            name="foundry_validate",
            description=(
                "Run a package's tests and check its structure without installing anything. "
                "Executes generated code, so it needs operator opt-in and approval."
            ),
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False,
            },
            handler=_foundry_async_only,
            risk="high",
        ),
        SandboxTool(
            name="foundry_install",
            description=(
                "Validate a package and install it as a reusable capability. A fresh "
                "install is registered disabled and unapproved; an identical reinstall "
                "preserves the operator's existing enable/approval flags. The report states "
                "the actual resulting state — trust that and attempt the call rather than "
                "assuming a gate. Executes generated code, so it needs opt-in and approval."
            ),
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False,
            },
            handler=_foundry_async_only,
            risk="high",
        ),
    ],
}


# The local servers a normal installation ships and seeds on every boot. These
# cannot be removed, only disabled. The `demo` catalog above is kept for the test
# and load harnesses, which opt in with TOOLLOOM_SEED_DEMO=1.
BUILTIN_SERVERS = ("sandbox",)


def server_names() -> list[str]:
    """Servers the product seeds on every boot."""
    return sorted(BUILTIN_SERVERS)


def available_local_servers() -> list[str]:
    """Every in-process catalog this build can materialize, seeded or not."""
    return sorted(LOCAL_SERVERS)


def tools_for(server_name: str) -> list[SandboxTool]:
    return list(LOCAL_SERVERS.get(server_name, []))


def effects_for(server_name: str) -> dict[str, str]:
    return {
        tool.name: EFFECTS.get(f"{server_name}__{tool.name}", "unknown")
        for tool in LOCAL_SERVERS.get(server_name, [])
    }


def get_tool(server_name: str, tool_name: str) -> SandboxTool | None:
    for tool in LOCAL_SERVERS.get(server_name, []):
        if tool.name == tool_name:
            return tool
    return None


def dispatch(server_name: str, tool_name: str, arguments: dict[str, Any], ctx: ToolContext) -> str:
    tool = get_tool(server_name, tool_name)
    if tool is None:
        raise SandboxError(f"unknown local tool {server_name}__{tool_name}")
    try:
        result = tool.handler(arguments, ctx)
    except (SandboxError, ToolPreflightError):
        raise
    except Exception as exc:  # convert handler bugs into reasoned tool errors
        raise SandboxError(f"{type(exc).__name__}: {exc}") from exc
    return result if isinstance(result, str) else json.dumps(result)
