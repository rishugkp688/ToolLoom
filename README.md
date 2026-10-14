# ToolLoom

ToolLoom is a local-first AI agent runtime. It owns the agent loop on your own
machine: it decides when to call a tool, runs it, reads the result, and keeps
going until it can answer. Every message, tool call, result and token count is
written to a local SQLite database you can open yourself.

It binds to `127.0.0.1` with no hosted backend, no account and no telemetry. The
browser is a thin client over REST and server-sent events. The backend is the
only process that talks to a model or to an MCP server, and the only place
credentials live.

![ToolLoom](Images/main_page_img.png)

## What it does

**Runs the loop in the backend.** Builds context, asks the model, streams the
answer, executes any requested tools, feeds the results back, and repeats until
the model stops or a budget is reached. The frontend never orchestrates and never
holds a provider key.

**Works with the models you have.** The model contract is provider neutral
(`ModelTurn` with text blocks, tool-call blocks, usage and a normalized stop
reason). Adapters cover the Command Code Provider API (`/chat/completions`,
`/messages`, `/models`), and a deterministic fake provider runs the entire test
suite with no key, no network and no cost.

**One tool contract, several sources.** Everything the model can call is a row in
the same catalogue, addressed as `server__tool`, validated against its JSON schema
again at execution time, gated by an enable flag and — for anything that reaches
the network, the OS, or code the agent wrote itself — a separate approval step.
Remote MCP servers (Streamable HTTP, legacy SSE, operator-approved stdio) are one
source. The built-in `sandbox` tools and the capabilities you build with Tool
Foundry are the others: they use the same registry, schema, risk level, approval
gate and executor, and simply skip the JSON-RPC round trip.

**Ships one built-in server.** `sandbox` provides workspace file tools, an
outbound fetch, keyless web search, optional Python, skill reading, and the Tool
Foundry authoring tools. Every tool starts disabled, and the tools that reach the
network, the OS or generated code also require explicit approval. A second
runtime-owned server, `generated`, hosts installed capabilities.

**Turns a script into a capability.** When you ask, the agent can scaffold a
package from a script you keep rewriting, fill in a CLI whose flags match a
declared schema, and make its tests pass. Only then can you install it, and an
installed capability arrives disabled and unapproved with its source available for
review. See [Capabilities](#capabilities-tool-foundry).

**Gives each conversation a workspace.** The agent reads, writes, greps and
deletes files in its own directory. You can attach files with the paperclip, and
download anything it produces — one file, or the whole workspace as a zip — from
the details panel. The panel lists the tools this session can call, grouped by the
server each came from, and lets you switch one off without leaving the conversation.

**Keeps the record honest.** Stop reasons, tool inputs and outputs, usage,
compaction and interruptions are persisted. Reload a conversation and you see
what actually ran, including failures and refusals. There are no invented tool
results.

## Quick start

```bash
# Python 3.12 via uv (https://docs.astral.sh/uv/)
uv python install 3.12
uv sync

# Point it at a provider
cp .env.example .env
#   then set COMMANDCODE_API_KEY in .env

# Run it
uv run uvicorn backend.main:create_app --factory --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>.

Run the test suite with no key and no network:

```bash
uv run pytest -q
uv run ruff check .
```

The suite covers the agent loop, both provider wire formats, MCP over HTTP, SSE
and stdio, the sandbox tools, Tool Foundry authoring, validation, installation and
tamper detection, workspace path safety, secret handling, compaction, resume,
concurrency limits and the SSE/API contract.

Two live harnesses are opt-in and hit the real provider, so they are marked
**NOT RUN** unless you set their flag:

```bash
TOOLLOOM_LIVE_SMOKE=1 uv run python tests/live_provider_smoke.py
TOOLLOOM_LIVE_FOUNDRY=1 uv run python tests/live_foundry_smoke.py
```

## Configuration

Everything is environment driven, documented in `.env.example`. Nothing
sensitive is committed.

| Variable | Default | Purpose |
| --- | --- | --- |
| `LLM_PROVIDER` | `commandcode` | `commandcode` for the real API |
| `COMMANDCODE_API_KEY` | none | Your key. Server-side only, never sent to the browser |
| `COMMANDCODE_MODEL` | `deepseek/deepseek-v4-flash` | Model id, or pick one from the `/models` catalogue in the UI |
| `COMMANDCODE_API_STYLE` | `openai` | `openai`, `anthropic`, or `auto` to route Claude models to `/messages` |
| `MODEL_CONTEXT_WINDOW` | auto | Optional smaller local cap. Blank uses the provider's catalogue |
| `TOOLLOOM_RUN_TIMEOUT_SECONDS` | `300` | Total deadline for one agent run |
| `TOOLLOOM_MAX_ITERATIONS` | `12` | Model turns per run, unless Unlimited steps is on |
| `TOOLLOOM_SEARCH_PROVIDER` | `duckduckgo` | `duckduckgo`, `wikipedia`, or `searxng` with `TOOLLOOM_SEARCH_URL` |
| `TOOLLOOM_ALLOW_PYTHON` | `0` | Enables the `python` tool. Read the warning below first |
| `TOOLLOOM_ALLOW_FOUNDRY` | `0` | Enables the Tool Foundry authoring tools, which run generated tests |
| `TOOLLOOM_ALLOW_STDIO` | `0` | Allows operator-configured local MCP commands |

## Tools

**Built-in `sandbox` server.** `fs_read`, `fs_write`, `fs_list`, `fs_grep`,
`fs_delete`, `http_fetch`, `web_search`, `python`, `skill_list`, `skill_read`, and
the Tool Foundry tools `foundry_scaffold`, `foundry_validate`, `foundry_install`.

**Web search** returns ranked titles, URLs and snippets so the model can read a
result properly with `http_fetch` instead of guessing URLs. No provider needs an
account or API key. DuckDuckGo is the default; Wikipedia is the stable keyless
alternative; SearXNG points at your own instance. Results are capped by
`TOOLLOOM_SEARCH_MAX_RESULTS` (default 5, ceiling 10) and the response body at
512 KB.

**MCP servers** come from a config you paste in Settings, using the same
`mcpServers` shape as other editors. Imports are all-or-nothing, new connections
arrive disabled, schema changes revoke earlier approval, and removing a custom
server asks first.

**Skills** are `SKILL.md` bundles installed from a zip or a single markdown file.
Discovery and reading are separate tools, and nothing is enabled until you say so.

**Secrets** live in an encrypted local vault. Reference them as
`${secrets.NAME}` in server headers or environment values, and they are resolved
server-side. The API returns a masked preview and never the value.

Enable and approve tools under **Settings → MCP**. A tool with `risk: high` needs
both, and the model cannot grant either one.

## Capabilities (Tool Foundry)

A capability is a script you keep rewriting, turned into a reviewed, tested tool
that every later conversation can call. The pipeline has three steps, and the
agent never starts it on its own — it runs when you ask for it.

1. **Author.** `foundry_scaffold` writes `foundry/<name>/` in the conversation
   workspace: a `manifest.json`, a CLI whose flags already match the schema you
   declared, and a unittest file. The agent fills in the logic and the tests.
2. **Validate.** `foundry_validate` checks the structure and runs the tests. Zero
   discovered tests counts as a failure, and so does any non-zero exit, so a
   package cannot be installed on an empty suite.
3. **Install.** `foundry_install` validates again, copies the package to
   `.local/tools/<name>/<version>/` — deliberately outside the agent-writable
   workspace — records the entrypoint's SHA-256, and registers it as
   `generated__<name>`, disabled and unapproved.

Review it under **Settings → Capabilities**: the declared permissions, the version,
the test count from install, and the source of both the CLI and its tests. Then
enable it, and approve it if it is high risk. There is no second permission system:
a capability is toggled with the same flags as any other tool.

Capabilities take workspace-relative paths, so `--input security-events.jsonl`
resolves against the conversation that calls them. If the installed file changes
after approval, its hash no longer matches and it refuses to run until you reinstall
it. Removing a capability deletes the installed package; the source stays in the
conversation that produced it.

Turn the feature on by setting `TOOLLOOM_ALLOW_FOUNDRY=1`, then enabling
`sandbox → foundry_scaffold` and enabling **and approving** `foundry_validate` and
`foundry_install`. Leave it off and the foundry tools refuse to do anything.

## Security and limits

This is a single-user local tool. It is honest about where the boundary is.

- The server binds `127.0.0.1`. `Host` is checked on every request, `Origin` on
  state-changing requests. No wildcard CORS.
- One active run per conversation, 16 globally. A competing run gets `409`,
  capacity exhaustion gets `429`, and the run deadline defaults to 300 seconds.
- Provider keys, the vault key and MCP headers never reach the model, the
  browser, a log line or a tool subprocess.
- `python` runs as your user with your filesystem and network access. The
  workspace is the working directory, not a jail. It is **not OS-isolated**.
  Leave it off unless you trust the workload.
- A capability is code you approved, and it runs with the same limits and the same
  lack of isolation. Validation executes the generated tests, which is why
  `TOOLLOOM_ALLOW_FOUNDRY` defaults to `0`, why installing needs approval, and why
  the installed copy lives outside the workspace the agent can rewrite. A
  manifest's `permissions` block is what the tool claims, not what the runtime
  enforces.
- `http_fetch` and `web_search` send data off the machine, so both are disabled
  and unapproved by default. `web_search` sends only the query text.
- Tool output, uploaded files and skills are treated as data, never as
  instructions.

## Project docs

[`Documents/architecture.md`](Documents/architecture.md) covers the runtime
contract, the request path, data model, tool execution, security boundaries and
the reasoning behind the main design choices.

## Layout

```text
backend/
  main.py            FastAPI app, lifespan, static UI
  config.py          environment to typed settings
  db.py              SQLAlchemy engine, session, SQLite pragmas
  api/               REST and SSE endpoints
  models/            ORM tables
  schemas/           Pydantic request and response types
  repositories/      SQLAlchemy data access
  runtime/           agent loop, step controller, observation transforms
  services/          context, tools, MCP, secrets, providers, sandbox
  services/tool_foundry/  capability manifests, validation, install store, runner
  tests/             offline test suite
frontend/            vanilla HTML, CSS and JS, served at /
.local/              git-ignored SQLite database, vault key, workspaces, tools
```
