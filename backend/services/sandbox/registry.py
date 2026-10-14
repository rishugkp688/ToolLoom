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
from backend.services.tool_foundry import authoring
from backend.services.tool_foundry.manifest import FoundryError


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
            name="python",
            description=(
                "Run Python in the conversation workspace. Executes with host OS "
                "permissions, NOT sandboxed. Requires operator opt-in and approval. "
                "Use print() for output; standard library and installed packages are available."
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
            name="fs_read",
            description="Read a text file from this conversation's workspace.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": _PATH,
                    "offset": {"type": "integer", "description": "1-based first line"},
                    "limit": {"type": "integer", "description": "number of lines"},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            handler=_fs_read,
        ),
        SandboxTool(
            name="fs_write",
            description="Create or append to a text file in the workspace.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": _PATH,
                    "content": {"type": "string"},
                    "mode": {"type": "string", "enum": ["write", "append"]},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            handler=_fs_write,
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
                "high-risk and disabled until an operator enables and approves it."
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
                "Validate a package and install it as a reusable capability. The tool is "
                "registered disabled and unapproved; the operator enables it in Settings -> "
                "Capabilities. Executes generated code, so it needs opt-in and approval."
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
    except SandboxError:
        raise
    except Exception as exc:  # convert handler bugs into reasoned tool errors
        raise SandboxError(f"{type(exc).__name__}: {exc}") from exc
    return result if isinstance(result, str) else json.dumps(result)
