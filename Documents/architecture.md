# ToolLoom architecture

The [2026-10-18 code and architecture review](code-architecture-review-2026-10-18.md)
records current verification and reproduced gaps in outcome tracking, approval,
context anchors and task state. Read it alongside the mechanism descriptions here;
the documented intent is stronger than the implementation in those failure paths.

This document explains what ToolLoom is, how a request travels through it, and
why the main pieces are built the way they are. It describes the code in this
repository as it stands. It is not a roadmap.

Where a mechanism has a known limit, the limit is stated next to the mechanism
rather than collected elsewhere, so no section here should be read as a guarantee
the code does not make.

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
- **Storage and control stay local.** The server binds loopback and holds the
  credentials. Configured inference sends context to Command Code; network and
  executable tools require separate operator enablement.

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
`TOOLLOOM_RUN_TIMEOUT_SECONDS` (300) and `TOOLLOOM_MAX_OUTPUT_CONTINUATIONS` (3).
Without that last one, a reply the provider cut off at the output limit ends the run
and the operator has to click Continue; with it, the run finishes the answer, and any
tool call from a truncated turn is refused rather than executed with partial
arguments. The **Unlimited steps** switch removes the two count caps for later runs,
and leaves the guards, timeouts, context limits and approvals in place.

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

Provider retries are bounded and apply to connection failures and HTTP
502/503/504 responses before streaming begins. An HTTP error does not prove that
upstream inference did no work, so this is not a no-double-charge guarantee.
Interrupted streaming is surfaced rather than silently restarted. Tool dispatch
has its own narrower retry policy.

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
| `shell` | high | Run a shell command. Not OS-isolated. The verification primitive |
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

**`shell` controls.** Off by default, and the reason the agent can check its own
work: run the test suite, read the failure, fix it. Without a primitive like this
an agent can write a project but never establish that it runs, which makes "done"
unfalsifiable.

It is a real shell (`bash`, else `sh`), so pipes, `&&` and substitution behave as
the operator expects. `find_posix_shell` returns `None` when neither exists, and
the tool is then neither advertised nor runnable rather than silently falling back
to `cmd`, whose semantics would change what a command means. Arguments are
validated before dispatch, the command length is bounded, `cwd` defaults to the
conversation workspace, and wall clock and combined output follow the same
`_exec` policy as `python` — including killing the whole process tree on expiry.

The child gets `minimal_env` rather than the operator's environment, so provider
keys and vault material cannot leak into a command, with `HOME` added because real
toolchains need it for caches and configuration. `cwd` may point outside the
workspace: the command could `cd` there anyway, so confining the argument would
be a false guarantee rather than a boundary. The tool is therefore documented the
same way `python` is — bounded, not contained — and is gated four times over:
`TOOLLOOM_ALLOW_SHELL`, the enable flag, the approval requirement, and removal
from the prompt when the flag is off.

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

Installation copies the package to `<data_dir>/tools/`, outside the
workspace-scoped filesystem API. This is not an OS boundary: opted-in Python and
generated code run as the same user, can read that directory, and can rewrite it
along with the records below. One version per name is active, and the new version
is published before the superseded one is retired, so an interrupted install
cannot lose the tool.

Approval is bound to the package **digest**, not to the tool's interface. Every
file is covered — entrypoint, helper modules, resources and the manifest — because
any of them can change what a package does. Consequences worth stating plainly:
reinstalling a package whose files changed revokes its approval and disables it,
so an unchanged schema can never re-authorise new code; a change to any file makes
it refuse to run; and a symlink inside a package fails the digest rather than being
followed out of it.

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


## 12. Durable execution and recovery

Four identities, deliberately distinct:

- **Conversation** — what was said. The `messages` table.
- **Task** — what is being accomplished: `original_request` (immutable), `objective`
  and `plan` (each versioned), and `completion_criteria`.
- **Run** — one attempt: status, stop reason, iteration and tool-call counts.
- **Tool attempt** — one dispatch: the tool, a digest of its arguments, the effect
  classification in force, and an outcome.

The loop is still owned by the streaming request. These rows make each attempt
*accountable*, not detached.

**Recorded before dispatch.** An attempt is written as `pending`, moved to `running`
at dispatch, and settled when the result is persisted, independently of the SSE
stream. A process that dies in between leaves a row rather than a gap.

**Recovered on boot.** A run left `running` becomes `interrupted`; an attempt left
`pending` or `running` becomes `unknown`. On resume the model receives the previous
run's status, the mutations already completed, and the outcomes that are uncertain,
so it continues from evidence instead of being told to carry on.

**No blind retries.** Re-dispatching an identical call whose outcome is `unknown` is
refused unless the tool is `read_only`, with a message naming the uncertainty and
suggesting a check first. Resolving it is an operator action
(`POST /conversations/{id}/attempts/{id}/reconcile`), because the runtime cannot know
whether the side effect landed.

**Effect classification is ours, not theirs.** `tools.effect` is
`read_only | idempotent | mutating | unknown`, defaults to `unknown`, and is
operator-controllable. It is never inferred from an MCP server's own description: a
server calling a tool read-only is stating an intent, not a guarantee. Our own reads
are pre-classified because we wrote them; `shell`, `python` and generated
capabilities stay `unknown`, which keeps them sequential and non-retryable.

**Plan state is pinned.** `plan_read` and `plan_write` maintain the task. The
rendered objective and plan go into the *system* prompt, outside the message-trimming
path, so no amount of summarisation can remove them. `plan.md` in the workspace is a
projection of the record, never a second authority.

## 13. Context management

`ContextPolicy` owns the whole fitting sequence and runs on every iteration. The
order is chosen by cost:

1. **Fit.** If the prompt fits, nothing happens. The provider's own
   `usage.input_tokens` for the previous prompt is trusted for that prefix and only
   messages appended since are estimated; the anchor is discarded when the system
   prompt or the tool catalogue changes.
2. **Prune tool results.** Old results are sliced to a head, a size note and a tail.
   Deterministic, no model call. The assistant's `tool_use` blocks are untouched, so
   the action history stays legible while its volume collapses.
3. **Summarise.** Only when pruning is not enough. `ContextCompactor` uses the same
   provider and keeps a verbatim recent suffix. A summary is accepted only on a
   completed turn, so a truncated one cannot make the messages it covered disappear.
4. **Drop the oldest exchanges.** The only step that truly loses information. A unit
   is one exchange — an assistant message together with the results answering it — so
   a run of a hundred tool calls is a hundred droppable units rather than one
   indivisible turn. The newest exchange is never dropped; when trimming leaves the
   transcript opening with an assistant message, an explicit note says earlier
   messages were dropped.
5. **Refuse.** If the system prompt, the tool schemas and the newest exchange alone
   exceed the window, no lossless path exists, so the run fails with a clear,
   recoverable error naming the budget. Silently truncating the operator's own
   message would be worse.

Repair and truncation are unchanged: `repair_orphan_tool_uses` never fabricates a
success, and oversized output is bounded as described in section 7. There is no
tokenizer dependency; estimates are approximate and the UI says so.

## 14. Concurrency

Run reservations are process-local: a set on the app state. The current
check/add/remove paths contain no await and execute on the event loop; there is
no separate lock around these transitions.

- One active run per conversation. A second request returns `409`.
- At most 16 active runs globally. Beyond that the request returns `429`.
- Deleting a conversation with an active run returns `409`.
- A cancelled or disconnected run releases its reservation, and the transcript
  records the cancellation.
- A run has a wall-clock deadline (300 seconds by default) and emits a resumable
  stop reason if it is reached.

One Uvicorn worker is the supported topology. The reservation set is in memory,
so multiple workers would not share it.

## 15. Security boundaries

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
  suite must run and pass. Installation copies the package outside the workspace
  filesystem API, and every run re-hashes the entrypoint against the value
  recorded at installation. That value is not yet bound to operator approval;
  imports and manifest changes are not covered by execution integrity.
- Tool output, uploaded files and skill bodies are data. The system prompt says
  so, and inlined skill bodies have `</` neutralized so a skill cannot close its
  own tags.

Stated rather than papered over:

- **`python` is not a sandbox.** It runs with your privileges. Time and output
  limits protect normal runs, not adversarial ones. Real isolation would need a
  container or a separate user account, which this project does not provide.
- **`shell` is not a sandbox either**, and confining it would be a lie rather than
  a boundary: any command can `cd` anywhere, so the tool says plainly that it runs
  with your privileges and leaves `cwd` unconfined. What it does provide is the
  ability to verify work instead of asserting it, which is why it exists. Time and
  output limits protect ordinary runs, not adversarial ones.
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

## 16. Frontend

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

## 17. Testing

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

## 18. Configuration

Grouped by purpose. See `.env.example` for the full list.

| Group | Variables |
| --- | --- |
| Provider | `LLM_PROVIDER`, `COMMANDCODE_API_KEY`, `COMMANDCODE_BASE_URL`, `COMMANDCODE_MODEL`, `COMMANDCODE_API_STYLE`, `COMMANDCODE_ZDR` |
| Budgets | `MODEL_MAX_OUTPUT_TOKENS`, `MODEL_CONTEXT_WINDOW`, `TOOLLOOM_MAX_ITERATIONS`, `TOOLLOOM_MAX_OUTPUT_CONTINUATIONS`, `TOOLLOOM_MAX_TOOL_CALLS`, `TOOLLOOM_RUN_TIMEOUT_SECONDS` |
| Server | `TOOLLOOM_HOST`, `TOOLLOOM_PORT`, `TOOLLOOM_DATA_DIR` |
| Capabilities | `TOOLLOOM_ALLOW_STDIO`, `TOOLLOOM_ALLOW_PYTHON`, `TOOLLOOM_PYTHON_TIMEOUT_SECONDS`, `TOOLLOOM_PYTHON_OUTPUT_BYTES` |
| Tool Foundry | `TOOLLOOM_ALLOW_FOUNDRY`, `TOOLLOOM_FOUNDRY_TIMEOUT_SECONDS`, `TOOLLOOM_FOUNDRY_TEST_TIMEOUT_SECONDS`, `TOOLLOOM_FOUNDRY_OUTPUT_BYTES` |
| Shell | `TOOLLOOM_ALLOW_SHELL`, `TOOLLOOM_SHELL_TIMEOUT_SECONDS`, `TOOLLOOM_SHELL_OUTPUT_BYTES` |
| Search | `TOOLLOOM_SEARCH_PROVIDER`, `TOOLLOOM_SEARCH_URL`, `TOOLLOOM_SEARCH_MAX_RESULTS`, `TOOLLOOM_SEARCH_TIMEOUT_SECONDS` |
| Behaviour | `TOOLLOOM_AUTO_TITLES`, `TOOLLOOM_SEED_DEMO` |
| Vault | `TOOLLOOM_SECRETS_KEY` |
| Cost reporting | `TOOLLOOM_PRICE_INPUT_PER_1K`, `TOOLLOOM_PRICE_OUTPUT_PER_1K` |

Invalid enum values fail at startup. Cost figures appear only when you supply
prices; token counts are always reported and no price is ever invented.

## 19. Deliberate non-goals

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
