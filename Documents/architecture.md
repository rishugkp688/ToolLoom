# ToolLoom architecture

This document explains what ToolLoom is, how a request travels through it, and
why the main pieces are built the way they are. It describes the code in this
repository as it stands. It is not a roadmap.

## 1. What ToolLoom is

ToolLoom is a single-user agent runtime that runs on your machine. The defining
choice is that it implements the agent loop itself instead of wrapping someone
else's agent framework or driving a CLI.

That decision buys four things:

- **The transcript is the product.** Every model turn, tool call, tool result,
  stop reason and token count is persisted, so a conversation can be replayed
  and audited.
- **Providers are replaceable.** The loop speaks one normalized contract, so
  swapping a model means writing an adapter, not touching the runtime.
- **Tools are uniform.** An in-process helper and a remote MCP server are
  discovered, validated and dispatched the same way.
- **Nothing leaves the box by default.** The server binds loopback, holds the
  credentials, and ships with every capability that reaches the network or the
  OS switched off.

The frontend is deliberately thin. It renders state and forwards input. It never
calls a model, never holds a key, and never decides what happens next.

## 2. Design principles

These are enforced by code, not by conventions:

1. **The backend owns orchestration.** The browser is a REST and SSE client.
2. **The provider contract is provider neutral.** OpenAI and Anthropic wire
   formats exist only inside adapters. The runtime, storage and UI never see a
   `choices` array or a `tool_use` content block from a vendor.
3. **MCP is the tool contract.** Tools come from a registry, are namespaced
   `server__tool`, and are dispatched through one executor.
4. **Default deny.** A newly discovered server, tool or skill is disabled. A
   high-risk tool also needs explicit approval, and the model can grant neither.
5. **Truth lives in backend state.** Nothing claims a tool ran without a
   persisted result. Missing results are recorded as missing.
6. **Budgets are bounded and visible.** Iterations, tool calls, wall clock,
   payload size and context have ceilings that surface as stop reasons.
7. **Local first.** SQLite, the loopback bind, no mandatory container, no cloud
   dependency, no telemetry.

## 3. Component map

Dependencies point one way: `api -> runtime/services -> repositories -> models`.

```text
                    Browser (vanilla HTML/CSS/JS, served at /)
                                    |
                        REST + SSE, same origin only
                                    v
                    FastAPI  (backend/main.py, backend/api/*)
                                    |
             +----------------------+----------------------+
             v                                             v
   ConversationService                            AgentRuntime (runtime/)
   (CRUD, message persistence)                    the agent loop
             |                                             |
             |                        +--------------------+--------------------+
             |                        v                                         v
             |                ContextBuilder / ContextCompactor          LLMProvider
             |                (services/)                               (services/providers/)
             |                        |                                         |
             |                SQLite transcript + workspace status     CommandCodeProvider
             |                        |                                 FakeProvider
             |                        |                                 future adapters
             |                        v
             |                 ToolExecutor (services/)
             |                        |
             |                 ToolRegistry (services/)
             |                        |
             |                 MCPConnectionManager (services/)
             |                   |            |            |
             |                   v            v            v
             |            local sandbox   Streamable    legacy SSE   stdio
             |            (in-process)      HTTP        (opt-in)      (opt-in)
             v
     SQLite (SQLAlchemy 2)  +  per-conversation workspace files
```

Key modules:

| Area | Module | Responsibility |
| --- | --- | --- |
| App | `backend/main.py` | App factory, lifespan, middleware, static mount |
| Config | `backend/config.py` | Environment to typed settings, enum validation |
| Storage | `backend/db.py` | Engine, session, SQLite pragmas, forward migrations |
| HTTP | `backend/api/*` | Thin endpoints: conversations, MCP, skills, secrets, settings, usage |
| Loop | `backend/runtime/agent_runtime.py` | The reasoning loop |
| Guards | `backend/runtime/step_controller.py` | Duplicate, cascade and flood detection |
| Output | `backend/runtime/observation.py` | Bounded tool output, spill references |
| Context | `backend/services/context_builder.py` | Prompt assembly, repair, budget |
| Context | `backend/services/context_compactor.py` | Summary of older turns through the provider |
| Tools | `backend/services/tool_registry.py` | Server and tool catalogue, discovery, import and export |
| Tools | `backend/services/tool_executor.py` | Authorization, validation, dispatch, retry |
| Tools | `backend/services/mcp_connection_manager.py` | Transport dispatch |
| Tools | `backend/services/mcp_client.py` | MCP SDK wrapper for HTTP, SSE and stdio |
| Sandbox | `backend/services/sandbox/*` | In-process tools and workspace rules |
| Providers | `backend/services/providers/*` | Normalized contract plus adapters |
| Secrets | `backend/services/secrets.py` | Fernet vault and `${secrets.NAME}` substitution |

## 4. The request path

A single streamed turn, from click to first token:

```text
POST /conversations/{id}/messages/stream
  |
  +-- validate Origin and Host
  +-- reserve the conversation (409 if a run is active, 429 if 16 are running)
  +-- persist the user message, auto-title if the conversation is untitled
  +-- return text/event-stream
        |
        v
   AgentRuntime.run_stream(conversation_id)
        |
        +-- compaction check (may emit compacting / compacted)
        +-- resume hint when the previous turn ended at a cliff
        |
        +-- for each iteration, up to the budget:
        |     ContextBuilder.build()      history + tools + skills + prompt
        |     provider.stream()           normalized deltas and tool calls
        |     persist the assistant turn
        |     if stop_reason == tool_use:
        |         ToolExecutor.execute() per call
        |         persist one tool result per call id
        |     else break
        |
        +-- emit done with reply, iterations, stop reason, usage, tool trace
```

The endpoint owns its own database session for the lifetime of the SSE
generator. A request-scoped session would close when the response headers are
sent, which is before the first event.

## 5. The agent loop

`AgentRuntime.run_stream` yields `AgentEvent` objects that map one to one onto
SSE frames:

| Event | Payload | When |
| --- | --- | --- |
| `compacting` | `{}` | Before the compaction check |
| `compacted` | `{}` | Only if a summary was actually written |
| `iteration_start` | `iteration` | Start of each model turn |
| `token` | `text` | Each streamed text delta |
| `tool_call_start` | tool call id, name | The model requested a tool |
| `tool_call_validated` | errors | Schema validation rejected the arguments before dispatch |
| `tool_call_retry` | attempt, reason | A safe pre-send failure is being retried |
| `tool_call_end` | input, output, error flag, attempts, duration | After the tool ran |
| `step_note` | level, message, context | A loop guard fired |
| `usage` | input, output, total tokens | End of a model turn |
| `done` | reply, iterations, stop reason, usage, tool trace | Turn finished |
| `error` | message | The run failed |

Normalized stop reasons are `end_turn`, `tool_use`, `max_tokens`,
`stop_sequence`, `max_iterations`, `cancelled` and `error`. A truncated response
is never reported as a clean finish: `length` becomes `max_tokens`, and a stream
that ends without a finish reason becomes `error`.

Ceilings: `TOOLLOOM_MAX_ITERATIONS` (12), `TOOLLOOM_MAX_TOOL_CALLS` (40),
`TOOLLOOM_RUN_TIMEOUT_SECONDS` (300) and `TOOLLOOM_MAX_PAYLOAD_BYTES`. The
**Unlimited steps** switch removes the two count caps for later runs, and leaves
the guards, timeouts, context limits and approvals in place.

`StepController` is deterministic and does no I/O. It flags three identical calls
with the same arguments, refuses a tool after three consecutive failures in one
turn, and notes a flood of six calls with no user-facing text. Its advice is
injected into the next iteration and is not persisted. It cannot grant access to
anything.

## 6. Provider contract

`backend/services/providers/base.py` defines the only shapes the runtime knows:

- `TextBlock`, `ToolCallBlock`, `ToolResultBlock` and `ProviderMessage`
- `ToolSpec` for advertised tools
- `ModelTurn` with ordered blocks, `stop_reason`, `Usage`, `model_id` and metadata
- Stream events: `TextDelta`, `ToolCallStartEvent`, `ToolCallDeltaEvent`,
  `UsageEvent`, `MessageStopEvent`
- `BaseProvider.stream()` for the wire, plus a `complete()` helper that drains it

`CommandCodeProvider` implements the documented endpoints. The OpenAI-compatible
path accumulates `delta.tool_calls[*]` by index because ids, names and argument
fragments arrive in separate chunks, and parses the JSON only once the call is
complete. The Anthropic-compatible path normalizes `tool_use`, `tool_result`,
text blocks, stop reasons and usage into the same contract. Style is chosen by
`COMMANDCODE_API_STYLE`, with `auto` routing Claude models to `/messages`.

Retries happen only on failures that provably did not reach the server, so a
retry cannot double-charge a request or replay an effect. If a response has
already begun streaming, an interruption is surfaced instead of silently
restarting.

`FakeProvider` implements the same contract deterministically. It supports
scripted turns, which is how the tool-use, interruption and resume tests drive
multi-step flows without a network or a key.

## 7. Tool execution

`ToolExecutor` is the only path from a model tool call to a result:

1. Split the namespaced name on the first `__`. A server name may not contain
   `__`, so a call routes to exactly one server.
2. Look up the server and confirm `enabled`.
3. Look up the tool and confirm `enabled`.
4. For `risk: high`, confirm `approved`.
5. Validate arguments against the stored JSON schema again. Not in the prompt,
   not once at discovery: here, before dispatch.
6. Dispatch through `MCPConnectionManager`.
7. Return an `ExecutionResult` for every failure mode.

Every failure becomes a structured error result rather than an exception, so the
model can read what went wrong and the transcript records it. Retry is limited to
pre-send transport failures (`ConnectError`, `ConnectTimeout`, `PoolTimeout`,
unwrapped from anyio exception groups), because those are the ones that cannot
have reached the server.

Output handling: results over 40,000 characters are shown to the model as a
bounded head-and-tail view, and the full text is written to
`<workspace>/.transcripts/<tool-call-id>.txt` with a path the model can page or
grep. The complete text still reaches the UI. Nothing is quietly dropped.

## 8. MCP integration

Four transports, one capability model:

| Transport | Path | Notes |
| --- | --- | --- |
| `local` | In-process | The built-in sandbox. Cannot be created or edited by an operator, or by the model |
| `http` | Streamable HTTP | Default for remote servers |
| `sse` | Legacy HTTP+SSE | Kept for servers that only offer it |
| `stdio` | Fixed local command | Operator-only, requires `TOOLLOOM_ALLOW_STDIO=1` |

Servers are stored in SQLite, not in a watched file. You paste a config in
Settings, or import one, and ToolLoom reconciles it:

- Imports are all-or-nothing. One invalid entry leaves every connection as it was.
- New and changed connections arrive disabled.
- A schema change revokes the previous approval for that tool.
- Discovery adds, updates and removes tool rows, and always preserves the enable
  flag on rows that already exist.
- Sync removal deletes omitted custom servers, never in-process ones.
- Exports mask literal headers, environment values and credential-bearing query
  strings as `<stored; unchanged>`, which round-trips safely in this installation
  and is deliberately not portable to another one.

## 9. The built-in sandbox

One in-process server named `sandbox`. It is always present, cannot be removed,
and every one of its tools starts disabled.

| Tool | Risk | What it does |
| --- | --- | --- |
| `fs_read`, `fs_write`, `fs_list`, `fs_grep`, `fs_delete` | low | Workspace file access |
| `http_fetch` | high | Fetch a public URL, with SSRF controls |
| `web_search` | high | Keyless search: DuckDuckGo, Wikipedia, or your own SearXNG |
| `python` | high | Run Python in the workspace. Not OS-isolated |
| `skill_list`, `skill_read` | low | Read installed skills |

**Workspace path rules.** Every path resolves through `Workspace.resolve`, which
rejects absolute paths, drive letters, UNC paths, `~`, NUL bytes, and anything
that escapes the root after `Path.resolve()`. A symlink planted by the model
cannot reach outside. Scratch directories (`.transcripts`, `.vendor`, `.cache`,
`.tmp`) are hidden from listings and downloads.

**`http_fetch` controls.** Only `http` and `https`. The hostname is resolved and
every address must be public, so loopback, private, link-local, multicast and
reserved ranges are refused. Literal `Authorization`, `Cookie`, `Host` and
`Proxy-Authorization` headers are rejected unless the whole value is one
`${secrets.NAME}` reference. The body is capped while streaming, redirects are
re-validated per hop and credentials are stripped when a redirect crosses origin.

**`web_search` controls.** No provider needs a key. The tool sends the query and
nothing else: headers are fixed by the tool, nothing is read from the vault, and
no cookies are kept. Markup parsing lives in three pure functions pinned by
fixture tests, so a provider changing its HTML fails loudly in the suite instead
of silently returning nothing.

**`python` controls.** Off by default. Runs `sys.executable` with the workspace
as the working directory and a minimal environment. 15-second default timeout and
a 64,000-byte combined output cap, both configurable.

## 10. Data model

SQLite through SQLAlchemy 2, with `foreign_keys=ON`, `journal_mode=WAL`,
`synchronous=NORMAL` and a 30-second busy timeout. WAL matters here because
parallel tool work reads the catalogue while the runtime writes the transcript.

| Table | Holds |
| --- | --- |
| `conversations` | Title, rolling summary, summary message count, timestamps |
| `messages` | Role, text, provider-neutral blocks as JSON, tool name, tool call id, stop reason, usage, resume flag |
| `mcp_servers` | Transport, URL, command and args, headers, enable and trust flags, timeouts |
| `tools` | Namespaced name, description, input schema, enable flag, risk, approval |
| `skills` | Name, description, on-disk path, enable and auto-include flags |
| `secrets` | Name, ciphertext, description |
| `settings` | Singleton row: global system prompt |

`create_all` only creates missing tables, so `db.py` also runs an idempotent
forward migration that inspects the schema and issues `ALTER TABLE ADD COLUMN`
for columns introduced after a database was first created. That is how an
existing `.local/toolloom.db` picks up new fields without data loss.

## 11. Context management

Four mechanisms keep a long conversation inside the model's window. The window
comes from the provider catalogue, and `MODEL_CONTEXT_WINDOW` can impose a
smaller local cap.

1. **Budget.** The builder reserves the response allowance plus estimation
   headroom, and drops the oldest complete logical turns until the rest fits. A
   turn is a user message that carries text plus everything up to the next one,
   so a tool call and its result are never split.
2. **Compaction.** When the pending transcript crosses the trigger,
   `ContextCompactor` summarizes older turns through the same provider, snapping
   the cutoff backwards to a user boundary so the newest turn is never folded in.
   The summary is stored on the conversation and prepended on later turns.
3. **Repair.** `repair_orphan_tool_uses` guarantees every `tool_use` block is
   followed by a matching `tool_result`. A missing result is injected as an
   explicit error reading `[no result available]`. Nothing is fabricated as a
   success.
4. **Truncation.** Oversized tool output is bounded as described in section 7.

There is no tokenizer dependency. Estimates are approximate and the UI says so.

## 12. Concurrency

Run reservations are process-local: a set on the app state, plus a lock around
transitions.

- One active run per conversation. A second request returns `409`.
- At most 16 active runs globally. Beyond that the request returns `429`.
- Deleting a conversation with an active run returns `409`.
- A cancelled or disconnected run releases its reservation, and the transcript
  records the cancellation.
- A run has a wall-clock deadline (300 seconds by default) and emits a resumable
  stop reason if it is reached.

One Uvicorn worker is the supported topology. The reservation set is in memory,
so multiple workers would not share it.

## 13. Security boundaries

Enforced in code:

- Loopback bind. `Host` is validated on every request, `Origin` on
  state-changing requests, and CORS is not opened with a wildcard.
- The provider key, vault key and MCP headers never reach the model, the
  browser, a log line or a tool subprocess environment.
- Secrets are encrypted at rest with Fernet. The key lives in a git-ignored file
  with `0600` permissions, or in `TOOLLOOM_SECRETS_KEY`. The API returns a masked
  preview and never the value.
- Skill archives are validated before extraction: no absolute paths, no `..`,
  no backslashes, no symlink entries, and a 25 MB expansion cap.
- Tool output, uploaded files and skill bodies are data. The system prompt says
  so, and inlined skill bodies have `</` neutralized so a skill cannot close its
  own tags.

Stated rather than papered over:

- **`python` is not a sandbox.** It runs with your privileges. Time and output
  limits protect normal runs, not adversarial ones. Real isolation would need a
  container or a separate user account, which this project does not provide.
- **`http_fetch` resolves the hostname before connecting** rather than pinning
  the validated address, so a DNS-rebinding race remains theoretically possible.
- **`web_search` reads an undocumented DuckDuckGo HTML endpoint.** That is a
  deliberate trade for keyless operation, and it can break when the markup
  changes. Wikipedia and SearXNG are the supported alternatives.
- Prompt formatting is defense in depth, not a security boundary. A model that
  follows instructions found in a web page is a model-level problem, bounded here
  by which tools are enabled and approved.

## 14. Frontend

Plain HTML, CSS and JavaScript served by FastAPI. No framework, no build step, no
CDN at runtime: the Geist variable font is bundled with its licence.

Three regions: a sessions rail, the conversation column, and a details panel that
opens on demand. Answers stream over `fetch` with a reader loop that parses SSE
frames; tool activity is grouped per turn and stays collapsed until opened, and
the final answer remains separate from the commentary around it. Markdown is
rendered by a small local module, and rendered text is escaped first.

Browser preferences (theme, model, motion) are stored locally. Transcript, usage,
tool execution, workspace files and credentials stay backend-owned.

## 15. Testing

The unit and integration suite runs offline: no key, no external network, no
container.

- Provider adapters are tested against scripted SSE chunks, including fragmented
  tool-call JSON, malformed frames and interrupted streams.
- MCP is tested against real local servers: a FastMCP instance over Streamable
  HTTP, one over legacy SSE, and a child process over stdio.
- The sandbox is tested for path traversal, symlink escape, malicious archives,
  secret exposure, output spilling and the search parsers.
- The loop is tested for tool round trips, refusal paths, duplicate call guards,
  cancellation, orphan repair, compaction and resume.
- API tests cover invalid input, missing ids, origin and host enforcement, and
  the concurrency limits.

Live checks that need a key, a browser or a paid call are opt-in and are not part
of the default run. They use temporary databases, so they never touch your
`.local/` data.

## 16. Configuration

Grouped by purpose. See `.env.example` for the full list.

| Group | Variables |
| --- | --- |
| Provider | `LLM_PROVIDER`, `COMMANDCODE_API_KEY`, `COMMANDCODE_BASE_URL`, `COMMANDCODE_MODEL`, `COMMANDCODE_API_STYLE`, `COMMANDCODE_ZDR` |
| Budgets | `MODEL_MAX_OUTPUT_TOKENS`, `MODEL_CONTEXT_WINDOW`, `TOOLLOOM_MAX_ITERATIONS`, `TOOLLOOM_MAX_TOOL_CALLS`, `TOOLLOOM_MAX_PARALLEL_TOOLS`, `TOOLLOOM_MAX_PAYLOAD_BYTES`, `TOOLLOOM_RUN_TIMEOUT_SECONDS` |
| Server | `TOOLLOOM_HOST`, `TOOLLOOM_PORT`, `TOOLLOOM_DATA_DIR` |
| Capabilities | `TOOLLOOM_ALLOW_STDIO`, `TOOLLOOM_ALLOW_PYTHON`, `TOOLLOOM_PYTHON_TIMEOUT_SECONDS`, `TOOLLOOM_PYTHON_OUTPUT_BYTES` |
| Search | `TOOLLOOM_SEARCH_PROVIDER`, `TOOLLOOM_SEARCH_URL`, `TOOLLOOM_SEARCH_MAX_RESULTS`, `TOOLLOOM_SEARCH_TIMEOUT_SECONDS` |
| Behaviour | `TOOLLOOM_AUTO_TITLES`, `TOOLLOOM_SEED_DEMO` |
| Vault | `TOOLLOOM_SECRETS_KEY` |
| Cost reporting | `TOOLLOOM_PRICE_INPUT_PER_1K`, `TOOLLOOM_PRICE_OUTPUT_PER_1K` |

Invalid enum values fail at startup. Cost figures appear only when you supply
prices; token counts are always reported and no price is ever invented.

## 17. Deliberate non-goals

Each of these was considered and left out on purpose. They are not oversights.

- **No agent framework.** LangChain, LangGraph and similar would take ownership
  of the loop this project exists to own.
- **No multi-user or hosted mode.** There is no authentication, so remote
  exposure is out of scope until there is.
- **No RAG or vector store.** Workspace files, `fs_grep` and web search cover the
  retrieval needs of a single-user local agent.
- **No OS-level isolation for Python.** Claiming otherwise would be worse than
  not shipping it. It is opt-in, with the limitation stated where you turn it on.
- **No invented capabilities.** Models are listed from the provider catalogue,
  costs only from configured prices, and evaluation numbers never come from a
  simulated run.
