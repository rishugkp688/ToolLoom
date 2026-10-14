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

**Uses MCP for tools.** Register servers over Streamable HTTP, legacy SSE, or an
operator-approved local command. Tools are discovered via `tools/list`
namespaced as `server__tool`, checked against their JSON schema again at
execution time, and dispatched per call.

**Ships one built-in server.** `sandbox` provides workspace file tools, an
outbound fetch, keyless web search, optional Python, and skill reading. Every
tool starts disabled, and the tools that reach the network or the OS also require
explicit approval.

**Gives each conversation a workspace.** The agent reads, writes, greps and
deletes files in its own directory. You can attach files with the paperclip and
download anything it produces from the details panel.

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
and stdio, the sandbox tools, workspace path safety, secret handling, compaction,
resume, concurrency limits and the SSE/API contract.

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
| `TOOLLOOM_ALLOW_STDIO` | `0` | Allows operator-configured local MCP commands |

## Tools

**Built-in `sandbox` server.** `fs_read`, `fs_write`, `fs_list`, `fs_grep`,
`fs_delete`, `http_fetch`, `web_search`, `python`, `skill_list`, `skill_read`.

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
  tests/             offline test suite
frontend/            vanilla HTML, CSS and JS, served at /
.local/              git-ignored SQLite database, vault key, workspaces
```
