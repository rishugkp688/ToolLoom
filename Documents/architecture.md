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
             |                   |            |            |          |
             |                   v            v            v          v
             |            local sandbox   generated   Streamable  legacy SSE  stdio
             |            (in-process)  (in-process)     HTTP     (opt-in)    (opt-in)
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
| Sandbox | `backend/services/sandbox/*` | In-process tools, shared subprocess policy, workspace rules |
| Foundry | `backend/services/tool_foundry/*` | Capability manifests, validation, install store, execution |
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

Five transports, one capability model:

| Transport | Path | Notes |
| --- | --- | --- |
| `local` | In-process | The built-in sandbox. Cannot be created or edited by an operator, or by the model |
| `generated` | In-process | Installed Tool Foundry capabilities. Also runtime-owned |
| `http` | Streamable HTTP | Default for remote servers |
| `sse` | Legacy HTTP+SSE | Kept for servers that only offer it |
| `stdio` | Fixed local command | Operator-only, requires `TOOLLOOM_ALLOW_STDIO=1` |

`local` and `generated` are in-process adapters rather than MCP sessions. Discovery
reads a Python dict, or the installed manifests, instead of making a `tools/list`
round trip; dispatch calls the handler directly instead of `tools/call`. There is
no JSON-RPC, no `initialize` handshake and no session for either. They are
registered as servers anyway, because that is what gives them the same catalogue,
schema validation, enable flag, risk level and approval gate as a remote server —
one model to reason about, one executor to audit.

Servers are stored in SQLite, not in a watched file. You paste a config in
Settings, or import one, and ToolLoom reconciles it:

- Imports are all-or-nothing. One invalid entry leaves every connection as it was.
- New and changed connections arrive disabled.
- A schema change revokes the previous approval for that tool.
- Discovery adds, updates and removes tool rows, and always preserves the enable
  flag on rows that already exist.
- Sync removal deletes omitted custom servers, never in-process ones (`local` and
  `generated` are both exempt).
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
| `foundry_scaffold` | low | Write a capability package skeleton |
| `foundry_validate`, `foundry_install` | high | Run generated tests, then install |

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

## 10. Tool Foundry

Turns a script the operator keeps rewriting into an installed capability that any
later conversation can call. The agent never starts this on its own; it runs when
asked. Stage 4 of the original plan — the agent noticing a repetition and
*proposing* a capability — is deliberately not built, because there is no evidence
yet that suggestions would be welcome.

### 10.1 Three steps, kept separate

1. **`foundry_scaffold`** writes `<workspace>/foundry/<name>/` containing
   `manifest.json`, a CLI skeleton whose `argparse` flags are generated from the
   declared schema (underscores become hyphens), and a unittest file. Existing
   files are never overwritten, so calling it twice is safe.
2. **`foundry_validate`** checks the manifest, that the entrypoint stays inside the
   package, and that the tests path exists, then runs the suite with
   `python -m unittest discover`. The standard library runner is a deliberate
   choice: pytest is a development dependency here, and a capability has to
   validate on any installation. Zero discovered tests is a failure, as is any
   non-zero exit code, so an empty suite cannot pass.
3. **`foundry_install`** validates again, copies the package to
   `<data_dir>/tools/<name>/<version>/`, records the entrypoint's SHA-256,
   `installed_at`, the originating conversation and the test count in
   `.integrity.json`, and registers the tool on the `generated` server disabled and
   unapproved.

### 10.2 Why installed packages leave the workspace

The workspace is agent-writable through `fs_write`. A package kept there could be
rewritten after the operator had reviewed and approved it, which would make an
integrity hash meaningless. Installation copies the package out to
`<data_dir>/tools/`, where no agent tool can write. One version per name is active;
installing again replaces it and records the new version.

### 10.3 The manifest is a proposal, not an enforcement layer

`permissions.filesystem` and `permissions.network` record what the tool claims to
need, and the Capabilities view shows them for review. The runtime does not act on
them. An installed capability is a child process running as the operator with no
OS-level isolation, so a script that ignores `network: false` still reaches the
network. The fields are labelled as claims everywhere they appear rather than
presented as a guarantee the runtime cannot keep.

### 10.4 Execution

`generated` dispatches in `MCPConnectionManager` beside the other transports.
`ToolExecutor` has already validated the arguments against the stored schema, so the
runner only maps them onto flags:

- `--name value`, with underscores written as hyphens.
- Booleans become a bare `--flag` when true and are omitted when false.
- Arrays repeat the flag; objects are JSON-encoded.
- Properties are emitted in sorted order, so one call always produces one argv.

The child runs with the **conversation workspace as its working directory**, which
is what makes `--input security-events.jsonl` meaningful from a conversation, and
with the shared policy in `sandbox/_exec.py`: argv arrays only, a minimal
environment, a wall-clock timeout, a combined output cap enforced while reading,
and a process-tree kill on expiry. Before each run the entrypoint is re-hashed
against the recorded value; a mismatch refuses the call instead of running it. The
same policy module backs `python`, so there is one place where subprocess rules
live.

### 10.5 Gates

`TOOLLOOM_ALLOW_FOUNDRY` (default `0`) gates all three authoring tools, because
validation executes generated code. The tools also obey default-deny: `foundry_
scaffold` starts disabled, and the two that run generated code additionally
require approval. Installed capabilities are `risk: high` for the same reason. An
operator who never sets the flag can never install anything, which is the property
that makes it safe to let the agent write a capability on request at all.

### 10.6 Capabilities view and API

`GET /capabilities` returns each installed package with its version, declared
permissions, install test count, provenance and current enable/approval state, so
the view can show what was approved rather than recomputing it.
`GET /capabilities/{name}/source` returns the CLI and its tests for review.
`DELETE /capabilities/{name}` removes the installed package and re-syncs the
catalogue. Enable and approval are not reimplemented: they go through
`PATCH /mcp/servers/{id}/tools/{name}` like any other tool.

### 10.7 What this deliberately does not do

No skill generation, and no automatic capability creation. The `description` in the
manifest carries the "when to use this" guidance the model needs, which covers the
discovery problem Stage 3 was meant to solve without adding a second artifact to
keep in sync.

## 11. Data model

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

## 12. Context management

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

## 13. Concurrency

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

## 14. Security boundaries

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
- Generated capability packages are validated before installation: the manifest
  must parse, the entrypoint and tests must resolve inside the package, and the
  suite must run and pass. Installation copies the package to a directory no agent
  tool can write to, and every run re-hashes the entrypoint against the value
  recorded at approval.
- Tool output, uploaded files and skill bodies are data. The system prompt says
  so, and inlined skill bodies have `</` neutralized so a skill cannot close its
  own tags.

Stated rather than papered over:

- **`python` is not a sandbox.** It runs with your privileges. Time and output
  limits protect normal runs, not adversarial ones. Real isolation would need a
  container or a separate user account, which this project does not provide.
- **A capability has the same limits, and no more.** Validation executes generated
  tests and the installed tool executes generated code, both as your user. What
  the runtime adds is review before installation, a hash check against later edits,
  and the fact that nothing installs until an operator opts in.
- **`http_fetch` resolves the hostname before connecting** rather than pinning
  the validated address, so a DNS-rebinding race remains theoretically possible.
- **`web_search` reads an undocumented DuckDuckGo HTML endpoint.** That is a
  deliberate trade for keyless operation, and it can break when the markup
  changes. Wikipedia and SearXNG are the supported alternatives.
- Prompt formatting is defense in depth, not a security boundary. A model that
  follows instructions found in a web page is a model-level problem, bounded here
  by which tools are enabled and approved.

## 15. Frontend

Plain HTML, CSS and JavaScript served by FastAPI. No framework, no build step, no
CDN at runtime: the Geist variable font is bundled with its licence.

Three regions: a sessions rail, the conversation column, and a details panel that
opens on demand. Answers stream over `fetch` with a reader loop that parses SSE
frames; tool activity is grouped per turn and stays collapsed until opened, and
the final answer remains separate from the commentary around it. Markdown is
rendered by a small local module, and rendered text is escaped first.

The details panel lists the enabled tools and the workspace files. Both sections
are collapsed disclosures, and either can be acted on in place: a tool can be
disabled from its row, and the whole workspace downloads as a zip rather than one
file at a time. Tools are grouped by the server they came from — `sandbox`,
`generated`, or one of your own connections — so the panel mirrors Settings → MCP
and the origin of each capability is visible at a glance. The rail's workspace
block doubles as the settings entry point, which is where operators expect to
find it.

Browser preferences (theme, model, motion) are stored locally. The motion switch
forces reduced motion on top of the `prefers-reduced-motion` media query, which
CSS already honours on its own; when nothing is stored the switch reflects the
operating system's setting. Transcript, usage, tool execution, workspace files and
credentials stay backend-owned.

## 16. Testing

The unit and integration suite runs offline: no key, no external network, no
container.

- Provider adapters are tested against scripted SSE chunks, including fragmented
  tool-call JSON, malformed frames and interrupted streams.
- MCP is tested against real local servers: a FastMCP instance over Streamable
  HTTP, one over legacy SSE, and a child process over stdio.
- The sandbox is tested for path traversal, symlink escape, malicious archives,
  secret exposure, output spilling and the search parsers.
- Tool Foundry is tested through the real `ToolExecutor`: scaffolding, the test
  gate rejecting zero tests and failing tests, installation landing outside the
  workspace, flag mapping, tamper detection, removal, and the reserved-name rules
  for the `generated` server.
- The loop is tested for tool round trips, refusal paths, duplicate call guards,
  cancellation, orphan repair, compaction and resume.
- API tests cover invalid input, missing ids, origin and host enforcement, and
  the concurrency limits.
- Frontend assets are checked statically, because there is no build step to catch
  the mistakes that fail silently in a browser: every `var(--x)` in the stylesheet
  must be defined, every element id the script reaches for must exist, every icon
  name must have a path, the markup must be balanced, and the tab/panel wiring must
  agree in both directions. Each of these was verified by mutation — reintroducing
  the bug makes the corresponding check fail.

Live checks that need a key, a browser or a paid call are opt-in and are not part
of the default run. They use temporary databases, so they never touch your
`.local/` data.

## 17. Configuration

Grouped by purpose. See `.env.example` for the full list.

| Group | Variables |
| --- | --- |
| Provider | `LLM_PROVIDER`, `COMMANDCODE_API_KEY`, `COMMANDCODE_BASE_URL`, `COMMANDCODE_MODEL`, `COMMANDCODE_API_STYLE`, `COMMANDCODE_ZDR` |
| Budgets | `MODEL_MAX_OUTPUT_TOKENS`, `MODEL_CONTEXT_WINDOW`, `TOOLLOOM_MAX_ITERATIONS`, `TOOLLOOM_MAX_TOOL_CALLS`, `TOOLLOOM_MAX_PARALLEL_TOOLS`, `TOOLLOOM_MAX_PAYLOAD_BYTES`, `TOOLLOOM_RUN_TIMEOUT_SECONDS` |
| Server | `TOOLLOOM_HOST`, `TOOLLOOM_PORT`, `TOOLLOOM_DATA_DIR` |
| Capabilities | `TOOLLOOM_ALLOW_STDIO`, `TOOLLOOM_ALLOW_PYTHON`, `TOOLLOOM_PYTHON_TIMEOUT_SECONDS`, `TOOLLOOM_PYTHON_OUTPUT_BYTES` |
| Tool Foundry | `TOOLLOOM_ALLOW_FOUNDRY`, `TOOLLOOM_FOUNDRY_TIMEOUT_SECONDS`, `TOOLLOOM_FOUNDRY_TEST_TIMEOUT_SECONDS`, `TOOLLOOM_FOUNDRY_OUTPUT_BYTES` |
| Search | `TOOLLOOM_SEARCH_PROVIDER`, `TOOLLOOM_SEARCH_URL`, `TOOLLOOM_SEARCH_MAX_RESULTS`, `TOOLLOOM_SEARCH_TIMEOUT_SECONDS` |
| Behaviour | `TOOLLOOM_AUTO_TITLES`, `TOOLLOOM_SEED_DEMO` |
| Vault | `TOOLLOOM_SECRETS_KEY` |
| Cost reporting | `TOOLLOOM_PRICE_INPUT_PER_1K`, `TOOLLOOM_PRICE_OUTPUT_PER_1K` |

Invalid enum values fail at startup. Cost figures appear only when you supply
prices; token counts are always reported and no price is ever invented.

## 18. Deliberate non-goals

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
