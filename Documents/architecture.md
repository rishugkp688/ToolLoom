# ToolLoom architecture

ToolLoom is a local-first, single-user agent runtime. The backend owns the agent
loop, the browser is a thin REST/SSE client, and durable state lives in SQLite
plus a per-conversation workspace. This document describes the current
implementation: its components, contracts, request path and enforced boundaries.
Where a mechanism has a limit, the limit is stated with it.

## 1. Design principles

1. **The backend owns orchestration.** The browser renders state and forwards
   input. It never calls a model, holds a credential, or decides the next step.
2. **The model contract is provider-neutral.** Vendor wire formats exist only
   inside adapters. The runtime, storage and UI see normalized text blocks,
   tool-call blocks, usage and stop reasons.
3. **MCP is the tool contract.** Every callable capability is a row in one
   catalogue, namespaced `server__tool`, schema-validated at execution time and
   dispatched through one executor.
4. **Default deny.** New servers, tools and skills start disabled. High-risk
   tools also need explicit approval, and the model can grant neither.
5. **Truth lives in backend state.** Every model turn, tool dispatch, result,
   stop reason and usage figure is persisted. A tool is never claimed to have run
   without a recorded result.
6. **Budgets are bounded and visible.** Iterations, tool calls, wall clock,
   output size and context have ceilings that surface as stop reasons or errors.
7. **Local first.** SQLite, a loopback bind, no mandatory container, no cloud
   dependency and no telemetry.

## 2. Component map

Dependencies point one way: `api → runtime/services → repositories → models`.

```text
                    Browser (vanilla HTML/CSS/JS, served at /)
                                    |
                          REST + SSE, same origin
                                    v
                    FastAPI (backend/main.py, backend/api/*)
                                    |
             +----------------------+----------------------+
             v                                             v
   ConversationService                            AgentRuntime (runtime/)
   (CRUD, messages, files)                        the agent loop
             |                                             |
             |                        +--------------------+--------------------+
             |                        v                                         v
             |                ContextPolicy / ContextBuilder /         LLMProvider
             |                ContextCompactor (services/)            (services/providers/)
             |                        |                                         |
             |                SQLite transcript + workspace             CommandCodeProvider
             |                        |                                 FakeProvider
             |                        v
             |                 ToolExecutor (services/)
             |                        |
             |                 ToolRegistry (services/)
             |                        |
             |                 MCPConnectionManager (services/)
             |                   |          |          |          |
             |                   v          v          v          v
             |              local sandbox  generated  Streamable  legacy SSE  stdio
             |              (in-process)  (in-process)   HTTP      (opt-in)   (opt-in)
             v
     SQLite (SQLAlchemy 2) + per-conversation workspace files
```

| Area | Module | Responsibility |
| --- | --- | --- |
| App | `backend/main.py` | App factory, lifespan, middleware, static mount |
| Config | `backend/config.py` | Environment to typed settings, validation |
| Storage | `backend/db.py` | Engine, sessions, SQLite pragmas, forward migrations |
| HTTP | `backend/api/*` | REST and SSE endpoints |
| Loop | `backend/runtime/agent_runtime.py` | The reasoning loop and run record |
| Guards | `backend/runtime/step_controller.py` | Duplicate, cascade and flood detection |
| Output | `backend/runtime/observation.py` | Bounded output and `ExecutionResult` |
| Context | `backend/services/context_builder.py` | Prompt assembly, repair and fitting |
| Context | `backend/services/context_policy.py` | Fit → prune → summarise → drop → refuse |
| Context | `backend/services/context_compactor.py` | Rolling summary through the provider |
| Tools | `backend/services/tool_registry.py` | Server/tool catalogue, discovery, import/export |
| Tools | `backend/services/tool_executor.py` | Authorization, validation, dispatch, retry |
| Tools | `backend/services/mcp_connection_manager.py` | Transport dispatch and session ownership |
| Tools | `backend/services/mcp_client.py` | MCP SDK wrapper |
| Sandbox | `backend/services/sandbox/*` | In-process tools, subprocess policy, workspace rules |
| Foundry | `backend/services/tool_foundry/*` | Manifests, authoring, validation, install store, runner |
| Providers | `backend/services/providers/*` | Normalized contract, Command Code adapter, fake provider |
| Skills | `backend/services/skills/*`, `skill_service.py` | Discovery, validation, installation |
| Secrets | `backend/services/secrets.py`, `secret_service.py` | Fernet vault and `${secrets.NAME}` resolution |
| Async | `backend/utils/async_work.py` | Short blocking work kept off the event loop |

## 3. Request path

A streamed turn:

```text
POST /conversations/{id}/messages/stream
  |
  +-- middleware: Host check; Origin check for state-changing requests
  +-- reserve the conversation (409 if active; 429 if 16 runs are active)
  +-- persist the user message; schedule a title update
  +-- return text/event-stream
        |
        v
   AgentRuntime.run_stream(conversation_id)
        +-- resolve the task and start a Run record
        +-- resume advisory when the previous turn was interrupted
        +-- for each iteration, within budget:
        |     render pinned task/plan and execution state
        |     ContextPolicy.prepare()       fit / prune / summarise / drop
        |     provider.stream()             normalized deltas and tool calls
        |     persist the assistant turn
        |     execute requested tools and persist one result per call id
        |     update progress and loop-guard state
        +-- emit done (reply, iterations, stop reason, usage, tool trace)
```

The SSE endpoint owns its own database sessions for the life of the generator; a
request-scoped session would close when the response headers are sent. Title
generation runs as a bounded background task and never holds the run open. The
conversation has a lightweight `/meta` endpoint so the client can poll the title
without fetching the transcript.

## 4. Agent loop

`AgentRuntime.run_stream` yields `AgentEvent` objects that map to SSE frames:

| Event | Payload | When |
| --- | --- | --- |
| `compacting` | `{}` | A compaction check is starting |
| `compacted` | `{reduction}` | History was actually reduced |
| `iteration_start` | `iteration` | Start of a model turn |
| `token` | `{text}` | A streamed text delta |
| `tool_call_start` | call id, name | The model requested a tool |
| `tool_call_validated` | tool, errors | Schema validation rejected arguments |
| `tool_call_retry` | attempt, reason | A safe pre-send retry is happening |
| `tool_call_end` | input, output, flags, duration | A call settled |
| `step_note` | level, message, context | A loop guard fired |
| `usage` | input, output, total | End of a model turn |
| `done` | reply, iterations, stop reason, usage, trace | Run completed |
| `error` | message | Run failed |

Provider-normalized stop reasons are `end_turn`, `tool_use`, `max_tokens`,
`stop_sequence`, `max_iterations`, `max_tool_calls`, `loop_guard`, `cancelled`
and `error`. Run outcomes are `completed`, `failed`, `interrupted` and
`cancelled`; an empty completed turn is recorded as `empty_response` rather than
as a successful empty answer.

Default limits:

| Setting | Default | Meaning |
| --- | --- | --- |
| `TOOLLOOM_MAX_ITERATIONS` | 100 | Model turns per run |
| `TOOLLOOM_MAX_TOOL_CALLS` | 300 | Tool calls per run |
| `TOOLLOOM_RUN_TIMEOUT_SECONDS` | 1800 | Wall-clock deadline for a run |
| `TOOLLOOM_MAX_OUTPUT_CONTINUATIONS` | 3 | Automatic continuations of a truncated reply |
| `TOOLLOOM_MAX_OUTPUT_TOKENS` | auto | Per-turn output cap |
| `MODEL_CONTEXT_WINDOW` | auto | Optional local context ceiling |

The **Unlimited steps** setting removes the iteration and tool-call caps for later
runs. Timeouts, context limits, approvals and loop guards remain. A provider turn
stopped at the output limit is continued automatically; any tool call from that
truncated turn is refused rather than executed with partial arguments.

`StepController` is deterministic and does no I/O. It warns on the third
identical call, refuses a call after five identical calls without progress, warns
after three consecutive failures with the same arguments, and notes six tool calls
without a user-facing answer. Progress is measured from the workspace revision
before and after a call, so a shell edit followed by the same verification command
is not mistaken for a loop. Guard advice is injected into the next prompt and is
not persisted.

Blocking database, filesystem and context work runs through `run_blocking`, which
keeps those operations off the event loop and does not abandon an in-flight
transaction on request cancellation. A terminal outcome is committed before
`done` or `error` is yielded; closing the generator afterwards does not rewrite a
completed run as interrupted.

## 5. Provider contract

`backend/services/providers/base.py` defines the only shapes the runtime knows:

- `TextBlock`, `ToolCallBlock`, `ToolResultBlock`, `ProviderMessage`
- `ToolSpec` for advertised tools
- `ModelTurn` with ordered blocks, `stop_reason`, `Usage`, `model_id`, metadata
- Stream events: `TextDelta`, `ToolCallStartEvent`, `ToolCallDeltaEvent`,
  `UsageEvent`, `MessageStopEvent`
- `BaseProvider.stream()` plus `complete()`, which drains a stream

`CommandCodeProvider` implements the documented endpoints:

- OpenAI-compatible `/chat/completions`, accumulating fragmented
  `tool_calls[*]` arguments by index and parsing only when complete.
- Anthropic-compatible `/messages`, normalizing `tool_use`, `tool_result`, text,
  stop reasons and usage into the same contract.
- `COMMANDCODE_API_STYLE` selects the style; `auto` routes Claude models to
  `/messages`.
- SSE error frames and JSON error objects become typed, redacted `ProviderError`
  values. A pre-response 429/502/503/504 is retried once with a bounded
  `Retry-After` wait; partially streamed requests are never replayed.
- Provider function names are translated to valid wire names where a canonical
  MCP identity is too long or contains punctuation, with a reversible mapping.

`FakeProvider` implements the same contract deterministically. It supports
scripted turns, which is how the offline suite drives multi-step tool use,
interruption and resume without a network or a key.

## 6. Tool execution

`ToolExecutor` is the only path from a model tool call to a result:

1. Resolve the requested name once to a canonical `server__tool` identity. A bare
   name maps to its namespaced form only when exactly one enabled tool matches;
   ambiguity is refused.
2. Split the server and tool names on the first `__`.
3. Confirm the server and tool are enabled; a high-risk tool must also be
   approved.
4. Validate the arguments against the stored JSON schema again, immediately
   before dispatch.
5. Dispatch through `MCPConnectionManager`.
6. Return an `ExecutionResult` for every failure mode.

Execution outcomes keep separate facts:

- `is_error` — the call could not be executed, was refused, or was interrupted.
- `exit_code` — what a process returned when it ran.
- `verification_failed` — a process ran and exited non-zero.
- `outcome_uncertain` — the effect may have landed but could not be proven.
- `validation_errors` — schema failures, surfaced before dispatch.

Retries are limited to a verified pre-send transport failure and only for a
read-only tool. The transport reports a stage (`pre_send`, `dispatch`, `cleanup`);
only `pre_send` proves the request never reached the server. A cleanup error
after a received result does not discard that result. A local preflight failure
(`ToolPreflightError`) is a known non-effect, so a corrected retry is allowed.

Tool output over the model-facing budget is shown as a bounded head-and-tail view;
the full text is written to `<workspace>/.transcripts/<tool-call-id>.txt` with a
path the model can page or grep. Known provider and vault secrets are redacted
before output is persisted or spilled.

Effect classification is operator-controlled: `read_only`, `idempotent`,
`mutating` or `unknown` (the default). It drives retry safety and mutation
serialization. A server's own claim that a tool is read-only is not treated as a
guarantee.

## 7. MCP integration

Five transports share one capability model:

| Transport | Path | Notes |
| --- | --- | --- |
| `local` | In-process | The built-in sandbox. Runtime-owned |
| `generated` | In-process | Installed Tool Foundry capabilities. Runtime-owned |
| `http` | Streamable HTTP | Default for remote servers |
| `sse` | Legacy HTTP+SSE | For servers that only offer it |
| `stdio` | Fixed local command | Operator-only; needs `TOOLLOOM_ALLOW_STDIO=1` |

`local` and `generated` read a Python catalogue or installed manifests instead
of making a `tools/list` call, and invoke a handler directly instead of
`tools/call`. They are still registered as servers so they obtain the same
catalogue, schema validation, enable flag, risk level and approval gate.

Remote servers are stored in SQLite and reconciled from the Settings import:

- Imports are all-or-nothing; one invalid entry leaves every connection unchanged.
- New and changed connections arrive disabled.
- A schema change revokes prior approval for that tool.
- Discovery adds, updates and removes tool rows while preserving the enable flag
  on existing rows.
- Sync removal deletes omitted custom servers; runtime-owned servers are exempt.
- Exports mask literal headers, environment values and credential-bearing query
  strings. The masked values round-trip only on the same installation.

stdio sessions are owned by a dedicated actor task because the MCP SDK context
is bound to the task that entered it. Sessions are reused across calls, with one
absolute deadline covering acquisition, initialization, queue wait and dispatch;
cleanup has its own bounded wait. Configuration changes invalidate superseded
sessions, including when the registry mutation runs on a worker thread.
HTTP and SSE sessions remain per-call.

## 8. Built-in sandbox

The `sandbox` server is always present and cannot be removed. Every tool starts
disabled.

| Tool | Risk | Purpose |
| --- | --- | --- |
| `fs_read`, `fs_write`, `fs_list`, `fs_grep`, `fs_delete` | low | Workspace file access |
| `plan_read`, `plan_write` | low | Durable task objective and plan |
| `http_fetch` | high | Public HTTP(S) fetch with SSRF controls |
| `web_search` | high | Keyless search: DuckDuckGo, Wikipedia or SearXNG |
| `python` | high | Python subprocess, opt-in |
| `shell` | high | Shell command, opt-in; the verification primitive |
| `skill_list`, `skill_read` | low | Read enabled skills |
| `foundry_scaffold` | low | Write a capability package skeleton |
| `foundry_validate`, `foundry_install` | high | Run generated tests, then install |

**Workspace paths.** Every path resolves through `Workspace.resolve`, which
rejects absolute paths, drive letters, UNC paths, `~`, NUL bytes and anything
that escapes the root after symlink resolution. Paths under the runtime namespace
(`.toolloom/`) and scratch directories (`.transcripts`, `.vendor`, `.cache`,
`.tmp`) are hidden from listings and downloads. Workspace traversal prunes
dependency and build trees such as `node_modules`, `.venv`, `.git`, `dist` and
`build`.

**`http_fetch`.** Only `http`/`https`; the hostname is resolved and every address
must be public, so loopback, private, link-local, multicast and reserved ranges
are refused. Credential-bearing literal headers are rejected unless the whole
value is one `${secrets.NAME}` reference. The body is capped while streaming,
redirects are re-validated per hop, and credentials are stripped across origins.

**`web_search`.** No provider needs an account or API key. Only the query is sent;
headers are fixed by the tool, the vault is not read, and cookies are not kept.
DuckDuckGo's HTML endpoint is undocumented and may change; Wikipedia is the
stable fallback and SearXNG uses an operator-supplied instance.

**`python` and `shell`.** Both are off by default and require the environment
flag, the tool enable flag and (because they are high risk) approval. They run as
the operator and are **not OS-isolated**. `python` uses the backend interpreter
with a conversation workspace cwd and a minimal environment. `shell` uses `bash`
when available (with `pipefail`) and otherwise `sh`; it never falls back to
`cmd`. Its `cwd` may point outside the workspace because the command could `cd`
there anyway; the argument is not presented as a boundary.

**Subprocess policy.** `sandbox/_exec.py` is shared by Python, shell and Tool
Foundry: argv arrays only, a minimal environment, a hard wall-clock timeout, a
combined output cap enforced while reading, stdout/stderr retaining independent
head and tail views, and process-tree termination on expiry. On Linux, a fixed
process guardian uses `PR_SET_CHILD_SUBREAPER` to keep descendants attached to a
verifiable owner. Spawned jobs are recorded by PID and kernel start time; startup
recovery kills a recorded group only when the identity still matches. Other
platforms use normal timeout and cancellation behavior.

**Mutation ownership.** Local mutating tools run in a worker thread under a
per-workspace lease. The lease is owned by a task that waits for the actual
thread, so a cancelled request cannot release it mid-write and an older writer
cannot publish over a newer one. A queued operation cancelled before it starts is
removed rather than run later.

## 9. Tool Foundry

Tool Foundry turns a reusable script into an installed capability.

1. **Scaffold** — `foundry_scaffold` writes
   `<workspace>/foundry/<name>/` with `manifest.json`, a CLI skeleton whose flags
   match the declared schema, and a unittest file. Existing files are not
   overwritten.
2. **Validate** — `foundry_validate` checks the manifest, confirms the entrypoint
   and tests stay inside the package, and runs `python -m unittest discover`.
   Zero discovered tests is a failure, as is any non-zero exit.
3. **Install** — `foundry_install` validates again, copies the package to
   `<data_dir>/tools/<name>/<version>/`, records the entrypoint hash, a digest over
   every package file, the install time, source conversation and test count, and
   registers `generated__<name>` disabled and unapproved.

Approval is bound to the package **digest**, not the interface. Every file is
covered: entrypoint, helpers, resources and manifest. Reinstalling changed code
revokes approval and disables the tool; a changed installed file refuses to run;
a symlink inside a package fails the digest instead of being followed.

New scaffolds use `argument_encoding: "json"`, an exact JSON envelope that
preserves empty arrays, booleans, objects, nulls and strings beginning with a
hyphen. Installed packages that declare `flags` keep their legacy CLI contract.
The runtime maps schema properties onto either form; it does not interpret the
manifest's `permissions` block, which records what a tool claims to need.

`GET /capabilities` lists installed packages with version, permissions, test
evidence, provenance and current enable/approval state.
`GET /capabilities/{name}/source` returns the source for review, and
`DELETE /capabilities/{name}` removes the package and re-syncs the catalogue.
Enablement and approval use the same tool endpoints as every other tool.

The feature is gated by `TOOLLOOM_ALLOW_FOUNDRY=1`. Validation executes generated
code, installation needs approval, and an installed capability runs as the
operator with no OS isolation.

## 10. Skills and secrets

**Skills.** A directory containing `SKILL.md` is a skill. Frontmatter supplies the
name, description, auto-include and hidden-from-model flags. Skills install from a
zip or a Markdown file; archives are validated before extraction (no absolute
paths, `..`, backslashes or symlink entries; bounded expansion). New skills start
disabled. Enabled skills appear in the model's prompt and can be read with
`skill_list` / `skill_read`. Inlined bodies have `</` neutralized so a skill
cannot close its own tags.

**Secrets.** Values are encrypted at rest with Fernet; the key lives in a
git-ignored file with private permissions or in `TOOLLOOM_SECRETS_KEY`. The API
returns masked previews and never the value. `${secrets.NAME}` references in MCP
headers or environment values resolve server-side. Vault contents are never sent
to the model.

## 11. Durable execution and recovery

Four identities are distinct:

- **Conversation** — what was said (`messages`).
- **Task** — what is being accomplished: immutable `original_request`, versioned
  `objective` and `plan`, completion criteria, and active/completed status.
- **Run** — one attempt: status, stop reason, iteration and tool-call counts.
- **Tool attempt** — one dispatch: canonical tool name, argument digest and JSON,
  effect classification, status and result digest.

An attempt is written before dispatch (`pending` → `running`) and settled when
the result is persisted. Settlement and the tool-result transcript row commit in
one transaction, so a crash between them cannot leave a settled attempt with no
recorded result.

On startup:

- A run left `running` becomes `interrupted`.
- An attempt left `pending` or `running` becomes `unknown`.
- Recorded Linux child jobs are terminated when their kernel identity still
  matches.

A resume receives the previous run's status, completed mutations and uncertain
outcomes, so it continues from evidence. Repeating an identical call whose outcome
is `unknown` is refused unless the tool is `read_only`; the operator resolves it
through `POST /conversations/{id}/attempts/{attempt_id}/reconcile`, optionally
with a note. Reconciliation is refused while the conversation has an active run or
a local writer is still finishing. The runtime cannot know whether an unknown side
effect landed.

Task state is authoritative in SQLite. The pinned prompt view carries the current
objective and plan; `.toolloom/plan.md` and `.toolloom/request.md` are runtime
projections, not a second source of truth. Completing a task ends its authority
over later runs.

## 12. Context management

`ModelCatalogue` resolves the selected model's context window and output
allowance. When `MODEL_MAX_OUTPUT_TOKENS` is blank, output is derived from the
window (half, capped at 32,768) and the remaining budget is reserved for the
prompt, with estimation headroom and a minimum input allowance. An explicit
output setting is a cap, never an entitlement to consume the whole window.

`ContextPolicy` runs on every iteration and reduces history in cost order:

1. **Fit.** If the prompt fits, nothing changes. The provider's own
   `usage.input_tokens` for the previous prompt anchors the prefix; appended
   messages are estimated.
2. **Prune tool results.** Old results become a head, a size note and a tail.
   Deterministic and free; assistant `tool_use` blocks remain intact.
3. **Summarise.** `ContextCompactor` uses the selected provider and keeps a recent
   verbatim suffix. A summary is accepted only on a completed turn and within its
   output contract; it advances the cursor only over complete exchanges.
4. **Drop oldest exchanges.** A unit is an assistant turn plus the results
   answering it. The newest exchange is never dropped. The fit decision includes
   any continuation notice, so the opening is not pushed over budget after the
   decision.
5. **Refuse.** If the system prompt, tool schemas and newest exchange alone exceed
   the window, the run fails with a clear `ContextBudgetError`.

The builder reads only the active suffix after the summary cursor, in bounded
chunks. Omitted unsummarized rows are reported and make the run summarise or state
the loss rather than silently pretending it saw everything. A single oversized
completed exchange is preserved in full as a `.transcripts/exchange-<digest>.json`
artifact and rendered as an explicit historical excerpt.

The pinned task view is a bounded rendering: objective, active/next steps, compact
completed evidence and a reference to the full plan. The durable task record is
never trimmed. `repair_orphan_tool_uses` supplies an explicit missing-result block
for an unpaired call; it never fabricates a successful result. Token counts are
character-based estimates, not exact model tokenization.

## 13. Data model

SQLite via SQLAlchemy 2, with `foreign_keys=ON`, `journal_mode=WAL`,
`synchronous=NORMAL` and a configurable busy timeout (1 second by default).
WAL allows reads while the runtime writes the transcript.

| Table | Holds |
| --- | --- |
| `conversations` | Title and title provenance, rolling summary and cursor, timestamps |
| `messages` | Role, text, provider-neutral block JSON, tool name/call id, stop reason, usage, user text, attachments, resume flag |
| `tasks` | Original request, objective and revisions, plan and revision, criteria, status |
| `runs` | Conversation, task, status, stop reason, error, iteration/tool counts, timestamps |
| `tool_attempts` | Run, call id, canonical name, argument digest/JSON, effect, status, result digest, reconciliation note/time |
| `mcp_servers` | Transport, URL/command/args/env, headers, enable/trust flags, timeouts |
| `tools` | Namespaced name, schema, description, enable/approval, risk, effect, package digest |
| `skills` | Name, description, path, enable/auto-include/hidden flags |
| `secrets` | Name, ciphertext, description |
| `settings` | Global system prompt and the Unlimited steps flag |

`create_all` creates missing tables; an idempotent forward migration adds columns
introduced after a database was first created, without losing data.

## 14. Concurrency and lifecycle

Reservations are process-local:

- One active run per conversation; a second request returns `409`.
- At most 16 active runs globally; beyond that returns `429`.
- Deleting a conversation with an active run returns `409`.
- A cancelled or disconnected run releases its reservation and records the stop.

A run has a wall-clock deadline (`TOOLLOOM_RUN_TIMEOUT_SECONDS`, default 1800s).
One Uvicorn worker is the supported topology; the reservation set is in memory,
so multiple workers would not share it. On shutdown, MCP sessions are closed and
the engine is disposed. Background title tasks hold a strong reference and are
bounded by their own deadline.

## 15. Frontend

Plain HTML, CSS and JavaScript served by FastAPI. No framework, build step or
runtime CDN; the bundled Geist font and local Markdown renderer work offline.

The UI has a sessions rail, a conversation column and an on-demand details panel.
Answers stream over `fetch` with an SSE reader. Intermediate commentary and tool
calls are grouped into one activity disclosure per turn; the final answer stays
separate. The details panel lists enabled tools grouped by server and the
workspace files, with in-place disable and download actions.

Browser preferences (theme, model, motion) are stored locally. Transcripts,
usage, tool execution, workspace files and credentials remain backend-owned. The
client polls the lightweight `/meta` endpoint for title updates rather than
re-fetching the conversation.

## 16. Security boundaries

Enforced in code:

- Loopback bind. `Host` is validated on every request and `Origin` on
  state-changing requests; CORS is not opened with a wildcard.
- Provider keys, the vault key and MCP credentials never reach the browser, the
  model or a tool subprocess environment.
- Secrets are Fernet-encrypted and returned only as masked previews.
- Skill archives are validated before extraction and bounded in size.
- Generated capability packages are validated, installed outside the workspace,
  digest-checked, and approval-bound to the digest.
- Tool output, uploaded files and skill bodies are treated as data. The system
  prompt says so, and inlined skill bodies are tag-neutralized.

Stated rather than implied:

- **`python` is not a sandbox.** It runs with your privileges. Time and output
  limits protect normal runs, not adversarial ones.
- **`shell` is not a sandbox either.** It runs as your user, and its `cwd` may
  point outside the workspace. Confining the argument would be a false boundary;
  the real controls are the opt-in flag, enablement and approval.
- **A capability has the same limits.** Validation and execution run generated
  code as your user. Review, digest binding and opt-in gates reduce risk; they do
  not provide OS isolation.
- **`http_fetch` resolves the hostname before connecting**, so a DNS-rebinding
  race remains theoretically possible.
- **`web_search` parses an undocumented DuckDuckGo HTML endpoint.** That is the
  trade for keyless operation; Wikipedia and SearXNG are the alternatives.
- Prompt formatting is defense in depth, not a security boundary. A model that
  follows instructions embedded in tool output is bounded by which tools are
  enabled and approved.

## 17. Configuration

Environment-driven; `.env.example` is the complete reference.

| Group | Variables |
| --- | --- |
| Provider | `LLM_PROVIDER`, `COMMANDCODE_API_KEY`, `COMMANDCODE_BASE_URL`, `COMMANDCODE_MODEL`, `COMMANDCODE_API_STYLE`, `COMMANDCODE_ZDR` |
| Budgets | `MODEL_MAX_OUTPUT_TOKENS`, `MODEL_CONTEXT_WINDOW`, `TOOLLOOM_MAX_ITERATIONS`, `TOOLLOOM_MAX_OUTPUT_CONTINUATIONS`, `TOOLLOOM_MAX_TOOL_CALLS`, `TOOLLOOM_RUN_TIMEOUT_SECONDS` |
| Server/data | `TOOLLOOM_HOST`, `TOOLLOOM_PORT`, `TOOLLOOM_DATA_DIR`, `TOOLLOOM_DB_BUSY_TIMEOUT_SECONDS` |
| Capabilities | `TOOLLOOM_ALLOW_STDIO`, `TOOLLOOM_ALLOW_PYTHON`, `TOOLLOOM_PYTHON_TIMEOUT_SECONDS`, `TOOLLOOM_PYTHON_OUTPUT_BYTES` |
| Tool Foundry | `TOOLLOOM_ALLOW_FOUNDRY`, `TOOLLOOM_FOUNDRY_TIMEOUT_SECONDS`, `TOOLLOOM_FOUNDRY_TEST_TIMEOUT_SECONDS`, `TOOLLOOM_FOUNDRY_OUTPUT_BYTES` |
| Shell | `TOOLLOOM_ALLOW_SHELL`, `TOOLLOOM_SHELL_TIMEOUT_SECONDS`, `TOOLLOOM_SHELL_OUTPUT_BYTES` |
| Search | `TOOLLOOM_SEARCH_PROVIDER`, `TOOLLOOM_SEARCH_URL`, `TOOLLOOM_SEARCH_MAX_RESULTS`, `TOOLLOOM_SEARCH_TIMEOUT_SECONDS` |
| Behaviour | `TOOLLOOM_AUTO_TITLES`, `TOOLLOOM_SEED_DEMO` |
| Vault | `TOOLLOOM_SECRETS_KEY` |
| Cost | `TOOLLOOM_PRICE_INPUT_PER_1K`, `TOOLLOOM_PRICE_OUTPUT_PER_1K` |

Invalid enum values fail at startup. Incompatible context/output settings fail
early with a precise error. Costs appear only when prices are configured; token
counts are always reported and no price is invented.

## 18. Testing

The default suite is offline: no key, no external network and no container. It
covers the agent loop, both provider wire formats, MCP over HTTP/SSE/stdio, the
sandbox and workspace rules, Tool Foundry authoring and installation, skills,
secrets, compaction, resume, concurrency limits and the SSE/API contract. It drives
the real `ToolExecutor` for tool-use, refusal, cancellation and recovery paths, and
checks the static UI assets without a build step.

Live checks are opt-in and use temporary databases, so they never touch the
operator's `.local/` data. The browser check requires Playwright:

```bash
TOOLLOOM_LIVE_SMOKE=1 uv run python tests/live_provider_smoke.py
TOOLLOOM_LIVE_FOUNDRY=1 uv run python tests/live_foundry_smoke.py
node tests/frontend_smoke.cjs
```

## 19. Deliberate non-goals

- **No agent framework.** The loop is the product; LangChain/LangGraph would take
  ownership of it.
- **No multi-user or hosted mode.** There is no authentication; remote exposure is
  out of scope until there is.
- **No RAG or vector store.** Workspace files, `fs_grep` and web search cover the
  retrieval needs of a single-user local agent.
- **No OS isolation for Python, shell or generated capabilities.** The limitation
  is stated at each opt-in rather than papered over.
- **No invented data.** Models come from the provider catalogue, costs only from
  configured prices, and a tool result is never fabricated.
