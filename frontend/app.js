"use strict";

// ToolLoom front end — a thin REST/SSE client for the local workspace.
// The backend owns the loop, the transcript and the workspace. This file renders
// backend state and forwards input; it never talks to a model or an MCP server.

const state = {
  sessions: [],
  currentId: null,
  messages: [],
  models: [],
  modelId: localStorage.getItem("toolloom.model") || "",
  running: false,
  submitting: false,
  runModeSaving: false,
  promptLoadVersion: 0,
  runFinished: null,
  controller: null,
  filter: "",
  attachments: [],
  canContinue: false,
  lastStop: null,
  dialogOpen: false,
  dialogOpener: null,
  dialogTimer: null,
  drawer: null,
  toolsDirty: true,
  toolCache: null,
  pendingDelete: null,
  expandedServers: new Set(),
  mcpDraftDirty: false,
};

const $ = (id) => document.getElementById(id);

const ICON_PATHS = {
  close: '<path d="m6 6 12 12M18 6 6 18"/>',
  trash: '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4 4"/>',
  tools: '<path d="m8 3-5 5 5 5M16 11l5 5-5 5M14 5l-4 14"/>',
  layers: '<path d="m12 3 9 5-9 5-9-5 9-5ZM3 12l9 5 9-5M3 16l9 5 9-5"/>',
  sidebar: '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M9 4v16"/>',
  panel: '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M15 4v16"/>',
  settings: '<path d="M12 3 4 7.5v9L12 21l8-4.5v-9L12 3Z"/><circle cx="12" cy="12" r="3"/>',
  arrowUp: '<path d="M12 19V5m-6 6 6-6 6 6"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
  lightbulb: '<path d="M9 18h6M10 21h4M8 14a6 6 0 1 1 8 0c-1 1-1 2-1 2H9s0-1-1-2Z"/>',
  file: '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9l-6-6Zm0 0v6h6M8 13h8M8 17h5"/>',
  message: '<path d="M20 15a3 3 0 0 1-3 3H9l-5 3V6a3 3 0 0 1 3-3h10a3 3 0 0 1 3 3v9Z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1 1M18 18l1 1M5 19l1-1M18 6l1-1"/>',
  moon: '<path d="M20 14A8 8 0 0 1 10 4a8 8 0 1 0 10 10Z"/>',
  spark: '<path d="M12 3v5M12 16v5M4.5 12h5M14.5 12h5M7 7l3 3M14 14l3 3M17 7l-3 3M10 14l-3 3"/>',
  check: '<path d="m5 13 4 4L19 7"/>',
  code: '<path d="m9 8-5 4 5 4M15 8l5 4-5 4"/>',
  chevron: '<path d="m9 6 6 6-6 6"/>',
  power: '<path d="M12 3v9"/><path d="M6.5 6.5a8 8 0 1 0 11 0"/>',
};
function icon(name) {
  return `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON_PATHS[name] || ""}</svg>`;
}

// --- plumbing ----------------------------------------------------------------

async function api(path, options = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (body && body.detail) detail = Array.isArray(body.detail)
        ? body.detail.map(item => item.msg || "Invalid request").join("; ") : body.detail;
    } catch (_) {}
    throw new Error(detail);
  }
  if (res.status === 204) return null;
  const type = res.headers.get("content-type") || "";
  return type.includes("application/json") ? res.json() : res.text();
}

// One announcement per message, on exactly one live region:
// progress goes to the polite region, failures to the assertive one.
function announce(message) {
  const live = $("live");
  if (live) live.textContent = message;
}

function alertUser(message) {
  const alerts = $("alerts");
  if (alerts) alerts.textContent = message;
}

function toast(message, kind = "info") {
  const el = document.createElement("div");
  el.className = "toast";
  el.dataset.kind = kind;
  el.textContent = message;
  $("toasts").appendChild(el);
  if (kind === "error") alertUser(message);
  else announce(message);
  setTimeout(() => el.remove(), 7000);
}

// Loading copy names the actual work, and the container says it is busy.
function showLoading(host, message) {
  if (!host) return;
  host.setAttribute("aria-busy", "true");
  host.innerHTML = `<p class="hint" data-loading="true">${escapeHtml(message)}</p>`;
}

function clearLoading(host) {
  if (host) host.removeAttribute("aria-busy");
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

function timeOf(iso) {
  const d = iso ? new Date(iso) : new Date();
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function setState(next) {
  const chip = $("stateChip");
  chip.dataset.state = next;
  $("stateLabel").textContent = ({ idle: "Ready", running: "Working", done: "Complete", error: "Error" })[next] || next;
}

// Continue is one action with two homes: the status bar on wide screens, the
// composer's thumb zone on narrow ones. Only one is ever visible (CSS decides).
function setContinue(visible) {
  state.canContinue = Boolean(visible);
  for (const id of ["continueBtn", "continueBtnBench"]) {
    const el = $(id);
    if (el) el.hidden = !visible;
  }
}

// --- sessions rail -----------------------------------------------------------

function renderSessions() {
  const host = $("sessionList");
  host.innerHTML = "";
  $("sessionCount").textContent = state.sessions.length;
  const q = state.filter.trim().toLowerCase();
  const rows = state.sessions.filter((s) => !q || (s.title || "").toLowerCase().includes(q));
  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "hint";
    empty.style.padding = "var(--s2) var(--s3)";
    empty.textContent = q ? "No session matches that." : "Your next idea starts here.";
    host.appendChild(empty);
    return;
  }
  for (const session of rows) {
    const row = document.createElement("div");
    row.className = "session-row";

    const open = document.createElement("button");
    open.type = "button";
    open.className = "session";
    if (session.id === state.currentId) open.setAttribute("aria-current", "true");
    open.innerHTML = `${icon("message")}<span class="session-title">${escapeHtml(session.title || "Untitled session")}</span>`;
    open.addEventListener("click", () => openSession(session.id));

    const del = document.createElement("button");
    del.type = "button";
    del.className = "session-del";
    del.setAttribute("aria-label", `Delete session ${session.title || "Untitled"}`);
    del.innerHTML = icon("close");
    del.addEventListener("click", (event) => {
      event.stopPropagation();
      deleteSession(session.id);
    });

    row.append(open, del);
    host.appendChild(row);
  }
}

async function loadSessions() {
  showLoading($("sessionList"), "Reading sessions…");
  try {
    state.sessions = await api("/conversations");
  } finally {
    clearLoading($("sessionList"));
  }
  renderSessions();
}

async function newSession() {
  if (state.running) { state.controller.abort(); await state.runFinished; }
  const session = await api("/conversations", { method: "POST", body: JSON.stringify({}) });
  state.sessions.unshift(session);
  await openSession(session.id);
  $("promptInput").focus();
}

async function openSession(id) {
  if (state.running) { state.controller.abort(); await state.runFinished; }
  closeDrawers();
  state.currentId = id;
  state.messages = [];
  state.attachments = [];
  setContinue(false);
  renderChips();
  renderSessions();
  try {
    const detail = await api(`/conversations/${id}`);
    if (state.currentId !== id) return;
    state.messages = detail.messages;
    restoreStopState(detail.messages);
    $("sessionTitle").textContent = detail.title || "Untitled session";
    renderLog();
    await refreshInspector();
  } catch (err) {
    toast(err.message, "error");
  }
}

function deleteSession(id) {
  if (state.pendingDelete) return;
  const session = state.sessions.find((s) => s.id === id);
  if (!session) return;
  state.pendingDelete = { id, kind: "chat", opener: document.activeElement, busy: false };
  $("deleteTitle").textContent = "Delete this chat?";
  $("deleteDescription").textContent = "This will permanently delete the conversation and its workspace files. This can’t be undone.";
  $("confirmDeleteBtn").textContent = "Delete chat";
  $("deleteSessionName").textContent = session.title || "Untitled session";
  $("deleteError").hidden = true;
  $("deleteError").textContent = "";
  $("deleteDialog").showModal();
  $("cancelDeleteBtn").focus();
}

function closeDeleteDialog() {
  if (!state.pendingDelete || state.pendingDelete.busy) return;
  $("deleteDialog").close();
}

async function confirmDeleteSession() {
  const pending = state.pendingDelete;
  if (!pending || pending.busy) return;
  const { id } = pending;
  // One dialog serves chats, MCP servers and installed capabilities.
  const target =
    pending.kind === "server"
      ? { url: `/mcp/servers/${id}`, noun: "this server", label: "Remove server" }
      : pending.kind === "capability"
        ? {
            url: `/capabilities/${encodeURIComponent(id)}`,
            noun: "this capability",
            label: "Remove capability",
          }
        : { url: `/conversations/${id}`, noun: "this chat", label: "Delete chat" };
  $("deleteError").hidden = true;
  if (pending.kind === "chat" && state.running && state.currentId === id) {
    $("deleteError").textContent = "Stop the current run before deleting this chat.";
    $("deleteError").hidden = false;
    return;
  }
  pending.busy = true;
  $("deleteDialog").setAttribute("aria-busy", "true");
  $("cancelDeleteBtn").disabled = true;
  $("confirmDeleteBtn").disabled = true;
  $("confirmDeleteBtn").textContent = "Deleting…";
  try {
    await api(target.url, { method: "DELETE" });
  } catch (err) {
    $("deleteError").textContent = `Couldn’t delete ${target.noun}. ${err.message}`;
    $("deleteError").hidden = false;
    return;
  } finally {
    pending.busy = false;
    $("deleteDialog").removeAttribute("aria-busy");
    $("cancelDeleteBtn").disabled = false;
    $("confirmDeleteBtn").disabled = false;
    $("confirmDeleteBtn").textContent = target.label;
  }
  if (pending.kind === "server") {
    state.toolsDirty = true;
    closeDeleteDialog();
    await Promise.all([renderServers(), refreshInspector(), loadCurrentConfig(false)]);
    toast("Server removed.");
    return;
  }
  if (pending.kind === "capability") {
    closeDeleteDialog();
    await Promise.all([renderCapabilities(), refreshInspector()]);
    toast("Capability removed.");
    return;
  }
  if (state.currentId === id) {
    state.currentId = null;
    state.messages = [];
    state.attachments = [];
    renderChips();
    setContinue(false);
    setState("idle");
    updateReadout({});
    $("sessionTitle").textContent = "New session";
    renderLog();
  }
  state.sessions = state.sessions.filter((session) => session.id !== id);
  renderSessions();
  closeDeleteDialog();
  announce("Session deleted.");
  await Promise.all([refreshInspector(), loadUsage()]);
}

// --- run log -----------------------------------------------------------------

function resultsById(messages) {
  const map = new Map();
  for (const message of messages) {
    if (message.role === "tool") map.set(message.tool_call_id, message);
  }
  return map;
}

// A crash can persist a tool result whose call was never recorded. Those would
// otherwise be invisible, so they get their own turn.
function unpairedResults(messages) {
  const claimed = new Set();
  for (const message of messages) {
    if (message.role !== "assistant") continue;
    for (const block of message.blocks || []) {
      if (block.type === "tool_use") claimed.add(block.id);
    }
  }
  return messages.filter((m) => m.role === "tool" && !claimed.has(m.tool_call_id));
}

function unpairedTurn(results, index) {
  const li = document.createElement("li");
  li.className = "turn turn--tool";
  const gutter = document.createElement("div");
  gutter.className = "gutter";
  gutter.innerHTML = `<span class="idx num">${String(index).padStart(2, "0")}</span>
    <span class="role">tool</span><span class="time num">unpaired</span>`;
  const body = document.createElement("div");
  body.className = "turn-body";
  const wrap = document.createElement("div");
  wrap.className = "entries";
  for (const result of results) {
    wrap.appendChild(
      entryElement({
        name: result.tool_name || "tool result",
        input: {},
        state: isErrorResult(result) ? "error" : "ok",
        output: result.content,
      })
    );
  }
  body.appendChild(wrap);
  li.append(gutter, body);
  return li;
}

function isErrorResult(result) {
  const block = (result?.blocks || [])[0];
  return Boolean(block && block.is_error);
}

function entryElement(data) {
  const status = data.state;
  const glyph = status === "ok" ? "✓" : status === "error" ? "✕" : "…";
  const el = document.createElement("div");
  el.className = "entry";
  el.dataset.open = "false";

  const head = document.createElement("button");
  head.type = "button";
  head.className = "entry-head";
  head.setAttribute("aria-expanded", "false");
  head.innerHTML = `<span class="entry-name">${escapeHtml(data.name)}</span>
    <span class="entry-status" data-state="${status}"><span aria-hidden="true">${glyph}</span>${status}</span>
    <span class="entry-caret" aria-hidden="true">▸</span>`;

  const body = document.createElement("div");
  body.className = "entry-body";
  body.inert = true;
  body.innerHTML = `<div><div class="entry-pad">
    <div class="entry-key">input</div>
    <pre class="entry-pre">${escapeHtml(JSON.stringify(data.input || {}, null, 2))}</pre>
    <div class="entry-key">output</div>
    <pre class="entry-pre">${escapeHtml(data.output || "(waiting for the backend)")}</pre>
  </div></div>`;

  head.addEventListener("click", () => {
    const willOpen = el.dataset.open !== "true";
    el.dataset.open = String(willOpen);
    head.setAttribute("aria-expanded", String(willOpen));
    body.inert = !willOpen;
    head.querySelector(".entry-caret").textContent = willOpen ? "▾" : "▸";
  });

  el.append(head, body);
  return el;
}

function entryFromToolUse(block, result) {
  return {
    name: block.name,
    input: block.arguments || {},
    state: result ? (isErrorResult(result) ? "error" : "ok") : "running",
    output: result ? result.content : "",
  };
}

function turnElement(message, index) {
  const role = message.role === "user" ? "user" : "assistant";
  const li = document.createElement("li");
  li.className = `turn turn--${role}`;

  const gutter = document.createElement("div");
  gutter.className = "gutter";
  gutter.innerHTML = `<span class="idx num">${String(index).padStart(2, "0")}</span>
    <span class="role">${role === "user" ? "you" : "agent"}</span>
    <span class="time num">${timeOf(message.created_at)}</span>`;

  const body = document.createElement("div");
  body.className = "turn-body";
  if (role === "user") {
    if (message.attachments?.length) {
      const files = document.createElement("div");
      files.className = "message-attachments";
      for (const file of message.attachments) {
        const link = document.createElement("a");
        link.className = "message-attachment";
        link.href = `/conversations/${encodeURIComponent(state.currentId)}/workspace/download?path=${encodeURIComponent(file.path)}`;
        link.setAttribute("aria-label", `Download ${file.name}`);
        const copy = document.createElement("span");
        copy.className = "attachment-copy";
        const name = document.createElement("span");
        name.className = "attachment-name";
        name.textContent = file.name;
        const meta = document.createElement("span");
        meta.className = "attachment-meta";
        const size = file.size_bytes;
        meta.textContent = Number.isFinite(size)
          ? (size < 1024 ? `${size} B` : `${(size / 1024).toFixed(1)} KB`) + " · Download"
          : "Attached file · Download";
        copy.append(name, meta);
        link.innerHTML = icon("file");
        link.appendChild(copy);
        files.appendChild(link);
      }
      body.appendChild(files);
    }
    const text = message.user_text ?? message.content ?? "";
    if (text) {
      const bubble = document.createElement("div");
      bubble.className = "user-message-text";
      bubble.innerHTML = renderMarkdown(text);
      body.appendChild(bubble);
    }
  } else {
    const text = (message.blocks || [])
      .filter((b) => b.type === "text")
      .map((b) => b.text)
      .join("");
    body.innerHTML = renderMarkdown(text || message.content || "");
  }

  li.append(gutter, body);
  return { li, body };
}

function activityElement() {
  const element = document.createElement("details");
  element.className = "activity";
  const summary = document.createElement("summary");
  summary.className = "activity-summary";
  const content = document.createElement("div");
  content.className = "activity-content";
  element.append(summary, content);
  return { element, summary, content };
}

function renderLog() {
  const host = $("turns");
  host.innerHTML = "";
  $("logEmpty").hidden = state.messages.length > 0;
  $("starters").hidden = state.messages.length > 0;
  const results = resultsById(state.messages);
  let index = 0;
  let work = null;
  let toolCount = 0;
  let errorCount = 0;
  for (const message of state.messages) {
    if (message.role === "tool") continue;
    index += 1;
    if (message.role === "user") { work = null; toolCount = 0; errorCount = 0; }
    const { li, body } = turnElement(message, index);
    if (message.role === "assistant") {
      const calls = (message.blocks || []).filter((b) => b.type === "tool_use");
      if (calls.length) {
        const wrap = document.createElement("div");
        wrap.className = "entries";
        for (const block of calls) {
          wrap.appendChild(entryElement(entryFromToolUse(block, results.get(block.id))));
        }
        body.appendChild(wrap);
        if (!work) {
          work = activityElement();
          const shell = turnElement({role: "assistant", created_at: message.created_at}, index);
          shell.body.appendChild(work.element);
          host.appendChild(shell.li);
        }
        const step = document.createElement("div");
        step.className = "activity-step";
        step.append(...body.childNodes);
        work.content.appendChild(step);
        toolCount += calls.length;
        errorCount += calls.filter(call => isErrorResult(results.get(call.id))).length;
        work.summary.textContent = `Tool activity · ${toolCount} ${toolCount === 1 ? "call" : "calls"}${errorCount ? ` · ${errorCount} failed or skipped` : ""}`;
        continue;
      }
    }
    host.appendChild(li);
  }
  const orphans = unpairedResults(state.messages);
  if (orphans.length) {
    index += 1;
    host.appendChild(unpairedTurn(orphans, index));
  }
  scrollLog();
}

function scrollLog() {
  const log = $("log");
  log.scrollTop = log.scrollHeight;
}

// --- streaming ---------------------------------------------------------------

function parseSSE(buffer, onEvent) {
  let idx;
  while ((idx = buffer.indexOf("\n\n")) !== -1) {
    const chunk = buffer.slice(0, idx);
    buffer = buffer.slice(idx + 2);
    let event = "message";
    let data = "";
    for (const line of chunk.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data += line.slice(5).trim();
    }
    onEvent(event, data);
  }
  return buffer;
}

async function streamRun(url, payload, { userText = null, attachments = [] } = {}) {
  let finishRun;
  state.runFinished = new Promise(resolve => { finishRun = resolve; });
  const runSessionId = state.currentId;
  const controller = new AbortController();
  state.controller = controller;
  state.running = true;
  $("sendBtn").disabled = true;
  $("unlimitedTools").disabled = true;
  $("stopBtn").hidden = false;
  setContinue(false);
  setState("running");

  if (userText !== null) {
    state.messages.push({
      role: "user",
      content: userText,
      user_text: userText,
      attachments,
      blocks: [{ type: "text", text: userText }],
      created_at: new Date().toISOString(),
    });
    renderLog();
  }

  const live = document.createElement("li");
  live.className = "turn turn--assistant turn--live";
  live.innerHTML = `<div class="gutter"><span class="idx num">··</span><span class="role">agent</span><span class="time num">now</span></div>
    <div class="turn-body"></div>`;
  $("turns").appendChild(live);
  const body = live.querySelector(".turn-body");
  scrollLog();

  let text = "";
  let started = false;
  let terminal = false;
  let streamFailed = false;
  let entriesWrap = null;
  const pendingEntries = new Map();

  const activity = activityElement();
  activity.element.hidden = true;
  body.appendChild(activity.element);
  let liveToolCount = 0;
  let iterationHasTools = false;
  let textBody = document.createElement("div");
  body.appendChild(textBody);
  const paint = () => {
    textBody.innerHTML = renderMarkdown(text);
    scrollLog();
  };

  const handle = (event, raw) => {
    let data = {};
    try {
      data = JSON.parse(raw);
    } catch (_) { throw new Error("The server sent an invalid stream event. Please retry."); }
    if (event === "iteration_start") {
      if (iterationHasTools) {
        textBody = document.createElement("div");
        body.appendChild(textBody);
      }
      text = "";
      iterationHasTools = false;
      entriesWrap = null;
    } else if (event === "token") {
      started = true;
      text += data.text || "";
      paint();
    } else if (event === "tool_call_start") {
      activity.element.hidden = false;
      liveToolCount += 1;
      activity.summary.textContent = `Working · ${liveToolCount} tool ${liveToolCount === 1 ? "call" : "calls"}`;
      if (!iterationHasTools) activity.content.appendChild(textBody);
      iterationHasTools = true;
      if (!entriesWrap) {
        entriesWrap = document.createElement("div");
        entriesWrap.className = "entries";
        activity.content.appendChild(entriesWrap);
      }
      const pendingEntry = entryElement({ name: data.name, input: {}, state: "running", output: "" });
      pendingEntries.set(data.tool_call_id, pendingEntry);
      entriesWrap.appendChild(pendingEntry);
      announce(`Running tool ${data.name}`);
      scrollLog();
    } else if (event === "tool_call_end") {
      const resolved = entryElement({
        name: data.name,
        input: data.input || {},
        state: data.is_error ? "error" : "ok",
        output: data.output_text || "",
      });
      const pendingEntry = pendingEntries.get(data.tool_call_id);
      if (pendingEntry && pendingEntry.parentElement) pendingEntry.replaceWith(resolved);
      else if (entriesWrap) entriesWrap.appendChild(resolved);
      pendingEntries.delete(data.tool_call_id);
      announce(`Tool ${data.name} ${data.is_error ? "failed" : "finished"}`);
      scrollLog();
    } else if (event === "step_note") {
      toast(data.message || "Loop guard fired");
    } else if (event === "compacted") {
      announce("Earlier turns were compacted into a summary.");
    } else if (event === "done") {
      terminal = true;
      activity.summary.textContent = `Tool activity · ${liveToolCount} ${liveToolCount === 1 ? "call" : "calls"}`;
      updateReadout(data);
      state.lastStop = data.stop_reason;
      setContinue(["max_tokens", "max_iterations", "loop_guard"].includes(data.stop_reason));
      const failed = data.stop_reason === "error";
      setState(failed ? "error" : "done");
      if (failed) { streamFailed = true; setContinue(true); }
      announce(failed ? "The response was interrupted." : "Turn complete.");
    } else if (event === "error") {
      terminal = true;
      streamFailed = true;
      setContinue(true);
      setState("error");
      toast(data.message || "Run failed", "error");
    }
  };

  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    if (!res.ok || !res.body) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail.detail || `Run failed (${res.status})`);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      buffer = parseSSE(buffer, handle);
    }
    buffer += decoder.decode();
    if (buffer.trim()) parseSSE(buffer + "\n\n", handle);
    if (!terminal) throw new Error("Connection ended before the response completed. You can continue the conversation.");
    if (!started && !liveToolCount) body.innerHTML = "<p>(no output)</p>";
  } catch (err) {
    if (err.name === "AbortError") {
      setContinue(true);
      setState("idle");
      announce("Run stopped.");
    } else {
      streamFailed = true;
      setContinue(true);
      setState("error");
      toast(err.message, "error");
    }
  } finally {
    state.running = false;
    state.controller = null;
    $("sendBtn").disabled = false;
    $("unlimitedTools").disabled = false;
    $("stopBtn").hidden = true;
    setContinue(state.canContinue);
    live.classList.remove("turn--live");
    try { await refreshSession(runSessionId); }
    finally {
      if (streamFailed) { setState("error"); setContinue(true); }
      finishRun();
    }
  }
}

async function refreshSession(id) {
  if (id) {
    try {
      const detail = await api(`/conversations/${id}`);
      if (state.currentId === id) {
        state.messages = detail.messages;
        restoreStopState(detail.messages);
        $("sessionTitle").textContent = detail.title || "Untitled session";
        renderLog();
      }
    } catch (_) {}
  }
  await Promise.all([loadSessions(), refreshInspector(), loadUsage()]);
}

function restoreStopState(messages) {
  const last = messages.at(-1);
  if (!last?.stop_reason) return;
  state.lastStop = last.stop_reason;
  $("readStop").textContent = last.stop_reason;
  $("readStop").classList.remove("muted");
  setContinue(["max_tokens", "max_iterations", "loop_guard", "cancelled", "error"].includes(last.stop_reason));
}

// --- composer ----------------------------------------------------------------

function renderChips() {
  const host = $("chips");
  host.innerHTML = "";
  host.hidden = state.attachments.length === 0;
  state.attachments.forEach((file, index) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.innerHTML = `<span>${escapeHtml(file.name)}</span>`;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.setAttribute("aria-label", `Remove attachment ${file.name}`);
    remove.textContent = "✕";
    remove.addEventListener("click", () => {
      state.attachments.splice(index, 1);
      renderChips();
    });
    chip.appendChild(remove);
    host.appendChild(chip);
  });
}

async function uploadFiles(fileList) {
  if (!fileList.length) return;
  const id = await ensureSession();
  const form = new FormData();
  for (const file of fileList) form.append("files", file);
  const res = await fetch(`/conversations/${id}/files`, { method: "POST", body: form });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    toast(body.detail || "Upload failed", "error");
    return;
  }
  state.attachments.push(...body.files);
  renderChips();
  announce(`${body.files.length} file(s) attached.`);
  await refreshInspector();
}

async function ensureSession() {
  if (state.currentId) return state.currentId;
  const session = await api("/conversations", { method: "POST", body: JSON.stringify({}) });
  state.currentId = session.id;
  state.messages = [];
  await loadSessions();
  return session.id;
}

async function submitMessage() {
  const input = $("promptInput");
  const text = input.value.trim();
  if ((!text && !state.attachments.length) || state.running || state.submitting || state.runModeSaving) return;
  state.submitting = true;
  try {
    const id = await ensureSession();
    input.value = "";
    input.style.height = "auto";
    const attachments = state.attachments;
    const payload = { message: text, model_id: state.modelId || null, attachments: attachments.map(file => file.path) };
    state.attachments = [];
    renderChips();
    await streamRun(`/conversations/${id}/messages/stream`, payload, { userText: text, attachments });
  } finally { state.submitting = false; }
}

async function continueRun() {
  if (state.running || state.runModeSaving || !state.currentId) return;
  await streamRun(`/conversations/${state.currentId}/continue/stream?model_id=${encodeURIComponent(state.modelId)}`, {});
}

// --- inspector ---------------------------------------------------------------

function updateReadout(done) {
  const usage = done.usage || {};
  $("readStop").textContent = done.stop_reason || "—";
  $("readStop").classList.toggle("muted", !done.stop_reason);
  $("readIters").textContent = done.iterations ?? 0;
  $("readIn").textContent = usage.input_tokens ?? 0;
  $("readOut").textContent = usage.output_tokens ?? 0;
  $("readTools").textContent = (done.tool_calls || []).length;
}

function groupByServer(tools) {
  const groups = new Map();
  for (const tool of tools) {
    const key = tool.server_name || "unknown source";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(tool);
  }
  return groups;
}

async function refreshInspector() {
  const toolHost = $("toolList");
  if (state.toolsDirty || !state.toolCache) {
    showLoading(toolHost, "Reading registry…");
    try {
      const tools = [];
      for (const server of await api("/mcp/servers")) {
        if (!server.enabled) continue;
        for (const tool of await api(`/mcp/servers/${server.id}/tools`)) {
          // Keep the owning server so rows can be grouped by source, and so a row
          // can disable its tool in place.
          if (tool.enabled) {
            tools.push({ ...tool, server_id: server.id, server_name: server.name });
          }
        }
      }
      state.toolCache = tools;
      state.toolsDirty = false;
    } catch (_) {
      state.toolCache = null;
    } finally {
      clearLoading(toolHost);
    }
  }
  const tools = state.toolCache || [];
  toolHost.innerHTML = "";
  $("toolCount").textContent = tools.length ? String(tools.length) : "";
  if (!tools.length) {
    toolHost.innerHTML = '<p class="hint">None enabled. Open Settings → MCP.</p>';
  } else {
    // Grouped by source server, in the order the registry lists them, so the panel
    // mirrors Settings → MCP and it is obvious what came from where.
    for (const [serverName, group] of groupByServer(tools)) {
      const block = document.createElement("div");
      block.className = "tool-group";
      block.setAttribute("role", "group");
      block.setAttribute("aria-label", `${serverName}, ${group.length} enabled`);
      const head = document.createElement("div");
      head.className = "insp-subhead";
      head.innerHTML = `<span>${escapeHtml(serverName)}</span><span class="num">${group.length}</span>`;
      block.appendChild(head);
      for (const tool of group) {
        const row = document.createElement("div");
        row.className = "row";
        const name = document.createElement("code");
        // The server is the group heading now, so the row shows only the tool name.
        name.textContent = tool.namespaced_name.startsWith(`${serverName}__`)
          ? tool.namespaced_name.slice(serverName.length + 2)
          : tool.namespaced_name;
        const spacer = document.createElement("span");
        spacer.className = "spacer";
        const risk = document.createElement("span");
        risk.className = "tag";
        risk.dataset.risk = tool.risk;
        risk.textContent = tool.risk;
        const off = document.createElement("button");
        off.type = "button";
        off.className = "btn btn-quiet btn-icon";
        off.innerHTML = icon("power");
        off.setAttribute("aria-label", `Disable ${tool.namespaced_name}`);
        off.title = "Disable";
        off.addEventListener("click", () => {
          disableTool(tool).catch((err) => toast(err.message, "error"));
        });
        row.append(name, spacer, risk, off);
        block.appendChild(row);
      }
      toolHost.appendChild(block);
    }
  }

  const fileHost = $("fileList");
  if (!state.currentId) {
    fileHost.innerHTML = '<p class="hint">No session open.</p>';
    $("downloadAllBtn").hidden = true;
    return;
  }
  showLoading(fileHost, "Reading workspace…");
  try {
    const listing = await api(`/conversations/${state.currentId}/workspace`);
    clearLoading(fileHost);
    fileHost.innerHTML = "";
    $("downloadAllBtn").hidden = !listing.files.length;
    if (!listing.files.length) {
      fileHost.innerHTML = '<p class="hint">Workspace is empty.</p>';
    } else {
      for (const file of listing.files) {
        const row = document.createElement("div");
        row.className = "file-item";
        const link = document.createElement("a");
        link.href = `/conversations/${state.currentId}/workspace/download?path=${encodeURIComponent(file.path)}`;
        link.textContent = file.path;
        const size = document.createElement("span");
        size.className = "size num";
        size.textContent = `${file.size_bytes} B`;
        row.append(link, size);
        fileHost.appendChild(row);
      }
    }
  } catch (_) {
    clearLoading(fileHost);
    $("downloadAllBtn").hidden = true;
    fileHost.innerHTML = '<p class="hint">Workspace unavailable.</p>';
  }
}

async function disableTool(tool) {
  await api(`/mcp/servers/${tool.server_id}/tools/${encodeURIComponent(tool.name)}`, {
    method: "PATCH",
    body: JSON.stringify({ enabled: false }),
  });
  state.toolsDirty = true;
  await refreshInspector();
  if (!$("settingsDialog").hidden) await refreshPanel();
  toast(`${tool.namespaced_name} disabled.`);
}

async function downloadAllFiles() {
  if (!state.currentId) return;
  try {
    const response = await fetch(`/conversations/${state.currentId}/workspace/download-all`);
    if (!response.ok) {
      let detail = `Couldn’t download the workspace (HTTP ${response.status}).`;
      try {
        detail = (await response.json()).detail || detail;
      } catch (_) {}
      throw new Error(detail);
    }
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = `workspace-${state.currentId.slice(0, 8)}.zip`;
    link.click();
    URL.revokeObjectURL(url);
  } catch (err) {
    toast(err.message, "error");
  }
}

async function loadUsage() {
  try {
    const totals = (await api("/usage")).global_;
    $("usageLine").textContent =
      `tokens ${totals.total_tokens}` + (totals.estimated_cost != null ? ` · $${totals.estimated_cost}` : "");
  } catch (_) {}
}

async function loadModels() {
  try {
    state.models = await api("/models");
  } catch (_) {
    state.models = [];
  }
  const select = $("modelSelect");
  select.innerHTML = "";
  for (const model of state.models) {
    const option = document.createElement("option");
    option.value = model.id;
    option.textContent = model.label || model.id;
    select.appendChild(option);
  }
  if (!state.models.some((model) => model.id === state.modelId)) {
    state.modelId = state.models[0]?.id || "";
    localStorage.setItem("toolloom.model", state.modelId);
  }
  if (!state.models.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "No models available";
    select.appendChild(option);
  }
  select.disabled = !state.models.length;
  if (state.modelId) select.value = state.modelId;
  updateModelContext();
}

function updateModelContext() {
  const model = state.models.find(item => item.id === state.modelId);
  const label = $("modelContext");
  if (!model?.effective_context_window) {
    label.textContent = "Context limit unavailable";
    return;
  }
  const tokens = model.effective_context_window.toLocaleString();
  label.textContent = model.context_source === "fallback"
    ? `${tokens} token budget · model limit unknown`
    : `${tokens} token context${model.context_source === "override" ? " · configured cap" : ""}`;
  label.title = `Reply budget: ${model.max_output_tokens.toLocaleString()} tokens. Input token counts are estimated.`;
}

async function loadStatus() {
  try {
    const config = await api("/config");
    $("providerLine").textContent = config.provider_configured
      ? config.provider
      : `${config.provider} · no key`;
  } catch (_) {
    $("providerLine").textContent = "Connection unavailable";
  }
}

// --- settings dialog ---------------------------------------------------------

function focusables(root) {
  return Array.from(
    root.querySelectorAll(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    )
  ).filter((el) => el.offsetParent !== null || el === document.activeElement);
}

function openSettings() {
  state.dialogOpener = document.activeElement;
  state.dialogOpen = true;
  if (state.dialogTimer) {
    clearTimeout(state.dialogTimer);
    state.dialogTimer = null;
  }
  closeDrawers();
  $("settingsScrim").hidden = false;
  $("settingsDialog").hidden = false;
  requestAnimationFrame(() => {
    $("settingsScrim").dataset.open = "true";
    $("settingsDialog").dataset.open = "true";
  });
  const shell = $("shell");
  if (shell) shell.inert = true;
  $("closeDialogBtn").focus();
  refreshPanel().catch((err) => toast(err.message, "error"));
}

function closeSettings() {
  state.dialogOpen = false;
  $("settingsScrim").dataset.open = "false";
  $("settingsDialog").dataset.open = "false";
  if (state.dialogTimer) clearTimeout(state.dialogTimer);
  state.dialogTimer = setTimeout(() => {
    $("settingsScrim").hidden = true;
    $("settingsDialog").hidden = true;
    state.dialogTimer = null;
  }, 180);
  const shell = $("shell");
  if (shell) shell.inert = false;
  if (state.dialogOpener && state.dialogOpener.focus) state.dialogOpener.focus();
}

function trapDialogKeys(event) {
  if (!state.dialogOpen) return;
  if (event.key === "Escape") {
    event.preventDefault();
    closeSettings();
    return;
  }
  if (event.key !== "Tab") return;
  const items = focusables($("settingsDialog"));
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

function currentTab() {
  const active = document.querySelector("#panelTabs .tab[aria-selected='true']");
  return active ? active.dataset.tab : "mcp";
}

async function refreshPanel() {
  const tab = currentTab();
  if (tab === "mcp") await Promise.all([renderServers(), loadCurrentConfig(false)]);
  else if (tab === "skills") await renderSkills();
  else if (tab === "capabilities") await renderCapabilities();
  else if (tab === "secrets") await renderSecrets();
  else if (tab === "prompt") await renderPrompt();
}

function selectTab(tab) {
  for (const button of document.querySelectorAll("#panelTabs .tab")) {
    const on = button.dataset.tab === tab;
    button.setAttribute("aria-selected", String(on));
    button.tabIndex = on ? 0 : -1;
  }
  for (const panel of document.querySelectorAll("[data-panel]")) {
    panel.hidden = panel.dataset.panel !== tab;
  }
  return refreshPanel();
}

function switchControl(text, checked, action, isDisabled = () => false) {
  const label = document.createElement("label");
  label.className = "check switch-label";
  const input = document.createElement("input");
  input.type = "checkbox";
  input.className = "toggle-control";
  input.setAttribute("role", "switch");
  input.checked = checked;
  input.disabled = isDisabled();
  const copy = document.createElement("span");
  copy.textContent = text;
  input.addEventListener("change", async () => {
    const next = input.checked;
    input.dataset.pending = "true";
    input.disabled = true;
    try { await action(next); }
    catch (err) { input.checked = !next; toast(err.message, "error"); }
    finally {
      delete input.dataset.pending;
      input.disabled = isDisabled();
      if (input.disabled) input.checked = false;
    }
  });
  label.append(input, copy);
  return label;
}

function removeServer(server) {
  if (state.pendingDelete) return;
  state.pendingDelete = { id: server.id, kind: "server", opener: document.activeElement, busy: false };
  $("deleteTitle").textContent = "Remove this server?";
  $("deleteSessionName").textContent = server.name;
  $("deleteDescription").textContent = "This removes the connection and its tool permissions. Your conversations stay saved. You can add the server again using mcp.json.";
  $("confirmDeleteBtn").textContent = "Remove server";
  $("deleteError").hidden = true;
  $("deleteDialog").showModal();
  $("cancelDeleteBtn").focus();
}

async function renderServers() {
  const host = $("serverList");
  showLoading(host, "Reading servers…");
  try {
    const [servers, config] = await Promise.all([api("/mcp/servers"), api("/config")]);
    const listings = await Promise.all(servers.map(server =>
      api(`/mcp/servers/${server.id}/tools`).catch(err => ({error: err.message}))));
    host.innerHTML = "";
    $("stdioStatus").textContent = config.allow_stdio
      ? 'Stdio is available: use "command", "args", and optional "env" in mcp.json.'
      : "Stdio requires TOOLLOOM_ALLOW_STDIO=1 and a restart before importing commands.";
    for (const [index, server] of servers.entries()) {
      const tools = listings[index];
      const card = document.createElement("details");
      card.className = "server-card";
      card.open = state.expandedServers.has(server.id);
      card.addEventListener("toggle", () => {
        if (card.open) state.expandedServers.add(server.id); else state.expandedServers.delete(server.id);
      });
      const summary = document.createElement("summary");
      summary.innerHTML = `<span class="server-name">${escapeHtml(server.name)}</span><span class="tag">${server.transport === "local" ? "Built-in" : escapeHtml(server.transport)}</span><span class="server-state">${server.enabled ? "Enabled" : "Disabled"}</span>`;
      const body = document.createElement("div");
      body.className = "server-body";
      const toolControls = [];
      const disabledNote = document.createElement("p");
      disabledNote.className = "hint server-disabled-note";
      disabledNote.textContent = "All tools are off while this server is disabled. Turn it on to restore your selections.";
      const syncToolControls = () => {
        disabledNote.hidden = server.enabled;
        for (const {input, tool, field} of toolControls) {
          if (!server.enabled || input.dataset.pending !== "true") input.checked = server.enabled && tool[field];
          input.disabled = !server.enabled || input.dataset.pending === "true";
        }
      };
      const toolSwitch = (text, tool, field) => {
        const label = switchControl(text, server.enabled && tool[field], async value => {
          const updated = await api(`/mcp/servers/${server.id}/tools/${encodeURIComponent(tool.name)}`, {
            method: "PATCH", body: JSON.stringify({[field]: value}),
          });
          tool[field] = updated[field];
          syncToolControls();
          state.toolsDirty = true;
          await refreshInspector();
        }, () => !server.enabled);
        toolControls.push({input: label.querySelector("input"), tool, field});
        return label;
      };
      const actions = document.createElement("div");
      actions.className = "server-actions";
      actions.appendChild(switchControl(`Enable ${server.name}`, server.enabled, async enabled => {
        const updated = await api(`/mcp/servers/${server.id}`, {method:"PATCH",body:JSON.stringify({enabled})});
        server.enabled = updated.enabled;
        summary.querySelector(".server-state").textContent = server.enabled ? "Enabled" : "Disabled";
        syncToolControls();
        state.toolsDirty = true;
        await refreshInspector();
      }));
      for (const [action, text] of [["discover", "Discover tools"], ["test", "Test connection"]]) {
        const button = document.createElement("button");
        button.type = "button"; button.className = "btn btn-quiet"; button.textContent = text;
        button.setAttribute("aria-label", `${text} on ${server.name}`);
        button.addEventListener("click", async () => {
          button.disabled = true;
          try {
            const result = await api(`/mcp/servers/${server.id}/${action}`, {method:"POST"});
            state.toolsDirty = true;
            toast(action === "test" ? `Connected · ${result.tool_count} tools` : `Discovered ${result.tools.length} tools`);
            await Promise.all([renderServers(), refreshInspector()]);
          } catch (err) { toast(err.message, "error"); }
          finally { button.disabled = false; }
        });
        actions.appendChild(button);
      }
      if (server.transport !== "local") {
        const remove = document.createElement("button");
        remove.type = "button"; remove.className = "btn btn-quiet danger-text";
        remove.textContent = "Remove"; remove.setAttribute("aria-label", `Remove server ${server.name}`);
        remove.addEventListener("click", () => removeServer(server)); actions.appendChild(remove);
      }
      body.appendChild(actions);
      body.appendChild(disabledNote);
      if (tools.error || !tools.length) {
        const empty = document.createElement("p"); empty.className = "hint";
        empty.textContent = tools.error || "No tools discovered. Use Discover tools to load them.";
        body.appendChild(empty);
      } else for (const tool of tools) {
        const row = document.createElement("div"); row.className = "tool-setting";
        const description = document.createElement("div");
        description.innerHTML = `<code>${escapeHtml(tool.name)}</code><span class="tag" data-risk="${escapeHtml(tool.risk)}">${escapeHtml(tool.risk)}</span>`;
        const hint = document.createElement("p"); hint.className = "hint"; hint.textContent = tool.description;
        description.appendChild(hint); row.appendChild(description);
        const controls = document.createElement("div"); controls.className = "group-actions";
        controls.appendChild(toolSwitch(`Enable ${tool.name}`, tool, "enabled"));
        if (tool.risk === "high") {
          controls.appendChild(toolSwitch(`Approve ${tool.name}`, tool, "approved"));
        }
        row.appendChild(controls);
        if (tool.namespaced_name === "sandbox__python") {
          const warning = document.createElement("p"); warning.className = "tool-warning";
          warning.textContent = "Python runs with your OS permissions and can access host files and the network. It is not an isolated sandbox. " +
            (config.allow_python ? "Enable and approve only code you trust." : "Set TOOLLOOM_ALLOW_PYTHON=1 and restart to opt in, then enable and approve here.");
          row.appendChild(warning);
        }
        body.appendChild(row);
      }
      syncToolControls();
      card.append(summary, body); host.appendChild(card);
    }
  } finally { clearLoading(host); }
}

async function loadCurrentConfig(force = false) {
  if (state.mcpDraftDirty && !force) return;
  const current = await api("/mcp/config");
  if (state.mcpDraftDirty && !force) return;
  $("importText").value = JSON.stringify(current.config, null, 2);
  state.mcpDraftDirty = false;
}

async function renderSkills() {
  const host = $("skillList");
  showLoading(host, "Reading skills…");
  const skills = await api("/skills");
  clearLoading(host);
  host.innerHTML = "";
  if (!skills.length) host.innerHTML = '<p class="hint">No skills installed.</p>';
  for (const skill of skills) {
    const card = document.createElement("div");
    card.className = "card";
    const head = document.createElement("div");
    head.className = "card-head";
    const id = `skill-${skill.id}`;
    const box = document.createElement("input");
    box.type = "checkbox";
    box.className = "toggle-control";
    box.setAttribute("role", "switch");
    box.id = id;
    box.checked = skill.enabled;
    box.addEventListener("change", async () => {
      try {
        await api(`/skills/${skill.id}`, {
          method: "PATCH",
          body: JSON.stringify({ enabled: box.checked }),
        });
      } catch (err) {
        toast(err.message, "error");
        box.checked = !box.checked;
      }
    });
    const label = document.createElement("label");
    label.className = "check";
    label.htmlFor = id;
    label.innerHTML = `<strong>${escapeHtml(skill.name)}</strong>`;
    const del = document.createElement("button");
    del.type = "button";
    del.className = "btn btn-quiet btn-icon";
    del.setAttribute("aria-label", `Delete skill ${skill.name}`);
    del.innerHTML = icon("close");
    del.addEventListener("click", async () => {
      try {
        await api(`/skills/${skill.id}`, { method: "DELETE" });
        await renderSkills();
      } catch (err) {
        toast(err.message, "error");
      }
    });
    head.append(box, label, del);
    const desc = document.createElement("p");
    desc.className = "hint";
    desc.style.marginTop = "var(--s2)";
    desc.textContent = skill.description;
    card.append(head, desc);
    host.appendChild(card);
  }
}

async function renderCapabilities() {
  const host = $("capabilityList");
  showLoading(host, "Reading capabilities…");
  let payload;
  try {
    payload = await api("/capabilities");
  } catch (err) {
    clearLoading(host);
    host.innerHTML = `<p class="hint">Couldn’t read capabilities. ${escapeHtml(err.message)}</p>`;
    return;
  }
  clearLoading(host);
  host.innerHTML = "";

  const capabilities = payload.capabilities || [];
  const note = document.createElement("p");
  note.className = "hint";
  if (!payload.foundry_enabled) {
    note.textContent =
      "Tool Foundry is off. Set TOOLLOOM_ALLOW_FOUNDRY=1 in .env and restart before the agent can build a capability.";
  } else if (!capabilities.length) {
    note.textContent =
      "Nothing installed yet. When a script gets reused across sessions, ask the agent to turn it into a capability and install it.";
  } else {
    note.textContent =
      "Built by the agent from a script you asked it to reuse. Review the source, then enable and approve it.";
  }
  host.appendChild(note);
  if (!capabilities.length) return;

  for (const item of capabilities) {
    host.appendChild(capabilityCard(item, payload.server_id));
  }
}

async function setCapabilityFlag(serverId, name, fields) {
  if (!serverId) throw new Error("the generated-tool server is missing; restart the backend");
  await api(`/mcp/servers/${serverId}/tools/${encodeURIComponent(name)}`, {
    method: "PATCH",
    body: JSON.stringify(fields),
  });
  state.toolsDirty = true;
  await refreshInspector();
}

function _switchControl(id, checked, onChange) {
  const box = document.createElement("input");
  box.type = "checkbox";
  box.className = "toggle-control";
  box.setAttribute("role", "switch");
  box.id = id;
  box.checked = checked;
  box.addEventListener("change", () => {
    onChange(box).catch(async (err) => {
      toast(err.message, "error");
      box.checked = !box.checked;
      await renderCapabilities();
    });
  });
  return box;
}

function capabilityCard(item, serverId) {
  const card = document.createElement("div");
  card.className = "card";

  const head = document.createElement("div");
  head.className = "card-head";

  const toggleId = `cap-${item.name}`;
  const toggle = _switchControl(toggleId, item.enabled, (box) =>
    setCapabilityFlag(serverId, item.name, { enabled: box.checked })
  );
  const label = document.createElement("label");
  label.className = "check";
  label.htmlFor = toggleId;
  label.innerHTML = `<strong>${escapeHtml(item.name)}</strong><span class="tag">${escapeHtml(item.version)}</span>`;

  const risk = document.createElement("span");
  risk.className = "tag";
  risk.dataset.risk = item.risk;
  risk.textContent = item.risk;

  head.append(toggle, label, risk);

  if (item.risk === "high") {
    const approveId = `${toggleId}-approve`;
    const approve = _switchControl(approveId, item.approved, (box) =>
      setCapabilityFlag(serverId, item.name, { approved: box.checked })
    );
    const approveLabel = document.createElement("label");
    approveLabel.className = "check";
    approveLabel.htmlFor = approveId;
    approveLabel.textContent = "Approved";
    head.append(approve, approveLabel);
  }

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "btn btn-quiet btn-icon";
  remove.setAttribute("aria-label", `Remove capability ${item.name}`);
  remove.innerHTML = icon("close");
  remove.addEventListener("click", () =>
    deleteCapability(item.name, `${item.name} ${item.version}`)
  );
  head.appendChild(remove);

  const desc = document.createElement("p");
  desc.className = "hint";
  desc.style.marginTop = "var(--s2)";
  desc.textContent = item.description;

  const meta = document.createElement("p");
  meta.className = "hint";
  meta.textContent = [
    `${item.tests_passed ?? 0} test(s) passed at install`,
    `entrypoint ${item.entrypoint}`,
    `sha256 ${String(item.sha256 || "").slice(0, 12)}`,
  ].join(" · ");

  card.append(head, desc, meta);

  if (!item.integrity_ok) {
    const warn = document.createElement("p");
    warn.className = "hint";
    warn.textContent =
      "This file changed after it was approved. It will refuse to run until you reinstall it.";
    card.appendChild(warn);
  }

  if (item.source_conversation) {
    const open = document.createElement("button");
    open.type = "button";
    open.className = "btn btn-quiet";
    open.textContent = "Where it came from";
    open.addEventListener("click", () => {
      const id = item.source_conversation;
      closeSettings();
      openSession(id).catch((err) => toast(err.message, "error"));
    });
    card.appendChild(open);
  }

  const details = document.createElement("details");
  details.className = "config-panel";
  const summary = document.createElement("summary");
  summary.textContent = "Source and tests";
  const body = document.createElement("div");
  body.className = "rows";
  let loaded = false;
  details.addEventListener("toggle", () => {
    if (!details.open || loaded) return;
    loaded = true;
    body.innerHTML = '<p class="hint">Reading source…</p>';
    api(`/capabilities/${encodeURIComponent(item.name)}/source`)
      .then((payload) => {
        body.innerHTML = "";
        for (const [name, text] of Object.entries(payload.files || {})) {
          const pre = document.createElement("pre");
          pre.className = "entry-pre";
          pre.textContent = `// ${name}\n${text}`;
          body.appendChild(pre);
        }
        if (!body.childElementCount) body.innerHTML = '<p class="hint">No readable files.</p>';
      })
      .catch((err) => {
        body.innerHTML = `<p class="hint">Couldn’t read the source. ${escapeHtml(err.message)}</p>`;
      });
  });
  details.append(summary, body);
  card.appendChild(details);

  return card;
}

function deleteCapability(name, label) {
  if (state.pendingDelete) return;
  state.pendingDelete = {
    id: name,
    kind: "capability",
    opener: document.activeElement,
    busy: false,
  };
  $("deleteTitle").textContent = "Remove this capability?";
  $("deleteDescription").textContent =
    "The installed package and its tests are deleted. The source stays in the conversation that created it. This can’t be undone.";
  $("confirmDeleteBtn").textContent = "Remove capability";
  $("deleteSessionName").textContent = label;
  $("deleteError").hidden = true;
  $("deleteError").textContent = "";
  $("deleteDialog").showModal();
  $("cancelDeleteBtn").focus();
}

async function renderSecrets() {
  const host = $("secretList");
  showLoading(host, "Reading secrets…");
  const secrets = await api("/secrets");
  clearLoading(host);
  host.innerHTML = "";
  if (!secrets.length) host.innerHTML = '<p class="hint">No secrets stored.</p>';
  for (const secret of secrets) {
    const row = document.createElement("div");
    row.className = "row";
    const del = document.createElement("button");
    del.type = "button";
    del.className = "btn btn-quiet btn-icon";
    del.setAttribute("aria-label", `Delete secret ${secret.name}`);
    del.innerHTML = icon("close");
    del.addEventListener("click", async () => {
      try {
        await api(`/secrets/${encodeURIComponent(secret.name)}`, { method: "DELETE" });
        await renderSecrets();
      } catch (err) {
        toast(err.message, "error");
      }
    });
    row.innerHTML = `<code>${escapeHtml(secret.name)}</code>
      <span class="secret-preview">${escapeHtml(secret.preview || "")}</span><span class="spacer"></span>`;
    row.appendChild(del);
    host.appendChild(row);
  }
}

async function loadRunMode() {
  const toggle = $("unlimitedTools");
  try {
    const settings = await api("/settings");
    toggle.checked = Boolean(settings.unlimited_tools);
    toggle.disabled = state.running;
    $("runModeHelp").textContent = `Enabled tools only · ${settings.run_timeout_seconds}s timeout`;
    $("runModeHelp").title = "Removes step and tool-count limits. Tool permissions, repeated-call protection and Stop still apply. May use more tokens.";
  } catch (_) {
    toggle.disabled = true;
  }
}

async function renderPrompt() {
  const version = ++state.promptLoadVersion;
  $("promptText").disabled = true;
  $("promptSaveBtn").disabled = true;
  try {
    const settings = await api("/settings");
    if (version === state.promptLoadVersion) $("promptText").value = settings.system_prompt || "";
  } finally {
    if (version === state.promptLoadVersion) {
      $("promptText").disabled = false;
      $("promptSaveBtn").disabled = false;
    }
  }
}

async function importConfig() {
  const text = $("importText").value.trim();
  if (!text) { toast("Choose a file or enter a config first", "error"); return; }
  const button = $("importBtn");
  if (button.disabled) return;
  button.disabled = true;
  $("configFeedback").hidden = true;
  try {
    const body = await api(`/mcp/servers/import${$("importSync").checked ? "?sync=1" : ""}`, {method:"POST",body:text});
    const failed = (body.results || []).filter(result => !result.ok);
    if (!body.applied || failed.length) {
      $("configFeedback").textContent = "Nothing changed. " + failed.map(result => `${result.name}: ${result.error}`).join(" · ");
      $("configFeedback").dataset.kind = "error";
      $("configFeedback").hidden = false;
      return;
    }
    state.mcpDraftDirty = false;
    state.toolsDirty = true;
    $("configFeedback").textContent = `Saved ${body.results.length} server(s). Removed ${body.removed || 0}. Enable new or changed connections above.`;
    $("configFeedback").dataset.kind = "success";
    $("configFeedback").hidden = false;
    await Promise.all([renderServers(), refreshInspector(), loadCurrentConfig(true)]);
  } catch (err) {
    $("configFeedback").textContent = err.message;
    $("configFeedback").dataset.kind = "error";
    $("configFeedback").hidden = false;
  } finally { button.disabled = false; }
}

async function uploadSkill() {
  const input = $("skillFile");
  if (!input.files.length) {
    toast("Choose a .zip or .md file", "error");
    return;
  }
  const form = new FormData();
  form.append("file", input.files[0]);
  const res = await fetch("/skills/upload", { method: "POST", body: form });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    toast(body.detail || "Upload failed", "error");
    return;
  }
  toast(`Installed ${body.installed} skill(s)`);
  input.value = "";
  await renderSkills();
}

async function saveSecret() {
  const name = $("secretName").value.trim();
  const value = $("secretValue").value;
  if (!name || !value) {
    toast("Name and value are required", "error");
    return;
  }
  try {
    await api("/secrets", { method: "POST", body: JSON.stringify({ name, value }) });
    $("secretName").value = "";
    $("secretValue").value = "";
    announce("Secret saved.");
    await renderSecrets();
  } catch (err) {
    toast(err.message, "error");
  }
}

// --- drawers, theme, motion --------------------------------------------------

function closeDrawers() {
  $("rail").classList.remove("is-open");
  $("inspector").classList.remove("is-open");
  $("drawerScrim").hidden = true;
  $("railToggle").setAttribute("aria-expanded", "false");
  $("inspectorToggle").setAttribute("aria-expanded", "false");
  state.drawer = null;
  $("rail").inert = window.innerWidth < 768;
  $("inspector").inert = true;
  document.querySelector(".workbench").inert = false;
}

function drawerTrigger(which) {
  return $(which === "rail" ? "railToggle" : "inspectorToggle");
}

function toggleDrawer(which) {
  const el = $(which);
  const open = !el.classList.contains("is-open");
  closeDrawers();
  if (!open) return;
  el.classList.add("is-open");
  el.inert = false;
  document.querySelector(".workbench").inert = true;
  if (which === "inspector") $("rail").inert = true;
  $("drawerScrim").hidden = false;
  drawerTrigger(which).setAttribute("aria-expanded", "true");
  state.drawer = which;
  const first = el.querySelector("button, a[href], input, select, textarea");
  if (first) first.focus();
}

function closeDrawerAndRestore() {
  const which = state.drawer;
  closeDrawers();
  if (which) {
    const trigger = drawerTrigger(which);
    if (trigger) trigger.focus();
  }
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("toolloom.theme", theme);
  $("themeBtn").innerHTML = icon(theme === "dark" ? "sun" : "moon");
  $("themeBtn").setAttribute("aria-label", theme === "dark" ? "Use light theme" : "Use dark theme");
  $("themeBtn").title = theme === "dark" ? "Use light theme" : "Use dark theme";
}

function wire() {
  document.querySelectorAll("[data-icon]").forEach((el) => { el.innerHTML = icon(el.dataset.icon); });
  document.querySelectorAll("[data-settings-tab]").forEach((button) => {
    button.addEventListener("click", () => {
      openSettings();
      selectTab(button.dataset.settingsTab).catch((err) => toast(err.message, "error"));
    });
  });
  document.querySelectorAll("[data-prompt]").forEach((button) => {
    button.addEventListener("click", () => {
      $("promptInput").value = button.dataset.prompt;
      $("promptInput").dispatchEvent(new Event("input"));
      $("promptInput").focus();
    });
  });
  $("starterFile").addEventListener("click", () => $("fileInput").click());
  $("closeInspectorBtn").addEventListener("click", closeDrawerAndRestore);
  window.addEventListener("resize", () => { if (!state.dialogOpen && !state.pendingDelete) closeDrawers(); });
  closeDrawers();
  $("newSessionBtn").addEventListener("click", () => newSession().catch((err) => toast(err.message, "error")));
  $("sessionSearch").addEventListener("input", (event) => {
    state.filter = event.target.value;
    renderSessions();
  });
  $("composer").addEventListener("submit", (event) => {
    event.preventDefault();
    submitMessage().catch((err) => toast(err.message, "error"));
  });
  $("stopBtn").addEventListener("click", () => state.controller && state.controller.abort());
  $("continueBtn").addEventListener("click", () => continueRun().catch(err => toast(err.message, "error")));
  $("continueBtnBench").addEventListener("click", () => continueRun().catch(err => toast(err.message, "error")));
  $("attachBtn").addEventListener("click", () => $("fileInput").click());
  $("fileInput").addEventListener("change", (event) => {
    uploadFiles(Array.from(event.target.files)).catch((err) => toast(err.message, "error"));
    event.target.value = "";
  });
  $("modelSelect").addEventListener("change", (event) => {
    state.modelId = event.target.value;
    localStorage.setItem("toolloom.model", state.modelId);
    updateModelContext();
  });
  $("themeBtn").addEventListener("click", () => {
    applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
  });
  $("motionToggle").addEventListener("change", (event) => {
    const reduced = event.target.checked;
    document.documentElement.dataset.motion = reduced ? "reduced" : "full";
    localStorage.setItem("toolloom.motion", reduced ? "reduced" : "full");
  });
  $("railToggle").addEventListener("click", () => toggleDrawer("rail"));
  $("inspectorToggle").addEventListener("click", () => toggleDrawer("inspector"));
  $("drawerScrim").addEventListener("click", closeDrawerAndRestore);
  $("settingsBtn").addEventListener("click", openSettings);
  $("workspaceSettingsBtn").addEventListener("click", openSettings);
  $("downloadAllBtn").addEventListener("click", () => {
    downloadAllFiles().catch((err) => toast(err.message, "error"));
  });
  $("closeDialogBtn").addEventListener("click", closeSettings);
  $("settingsScrim").addEventListener("click", closeSettings);
  $("cancelDeleteBtn").addEventListener("click", closeDeleteDialog);
  $("confirmDeleteBtn").addEventListener("click", () => {
    confirmDeleteSession().catch((err) => toast(err.message, "error"));
  });
  $("deleteDialog").addEventListener("cancel", (event) => {
    if (state.pendingDelete?.busy) event.preventDefault();
  });
  $("deleteDialog").addEventListener("click", (event) => {
    const bounds = $("deleteDialog").getBoundingClientRect();
    if (event.target === $("deleteDialog") &&
        (event.clientX < bounds.left || event.clientX > bounds.right ||
         event.clientY < bounds.top || event.clientY > bounds.bottom)) closeDeleteDialog();
  });
  $("deleteDialog").addEventListener("close", () => {
    const opener = state.pendingDelete?.opener;
    state.pendingDelete = null;
    const fallback = state.dialogOpen ? $("closeDialogBtn") : state.drawer === "rail" ? $("newSessionBtn") : $("promptInput");
    (opener?.isConnected && opener.offsetParent !== null ? opener : fallback).focus();
  });
  document.addEventListener("keydown", (event) => {
    // Keep Tab on the dialog's actions instead of cycling through browser chrome.
    if ($("deleteDialog").open) {
      if (event.key === "Tab") {
        const items = focusables($("deleteDialog"));
        const first = items[0], last = items[items.length - 1];
        if (!items.length) event.preventDefault();
        else if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
      return;
    }
    if (state.dialogOpen) {
      trapDialogKeys(event);
      return;
    }
    if (state.drawer && event.key === "Tab") {
      const items = focusables($(state.drawer));
      const first = items[0], last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      return;
    }
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "j") {
      event.preventDefault();
      newSession().catch((err) => toast(err.message, "error"));
    }
    if (event.key === "Escape" && state.drawer) {
      event.preventDefault();
      closeDrawerAndRestore();
    }
  });
  $("panelTabs").addEventListener("click", (event) => {
    const tab = event.target.closest(".tab");
    if (tab) selectTab(tab.dataset.tab).catch((err) => toast(err.message, "error"));
  });
  $("panelTabs").addEventListener("keydown", (event) => {
    const tabs = Array.from($("panelTabs").querySelectorAll(".tab"));
    const index = tabs.indexOf(document.activeElement);
    if (index < 0 || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1
      : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    tabs[next].focus();
    selectTab(tabs[next].dataset.tab).catch((err) => toast(err.message, "error"));
  });
  $("importBtn").addEventListener("click", importConfig);
  $("importText").addEventListener("input", () => { state.mcpDraftDirty = true; });
  $("importFileBtn").addEventListener("click", () => $("mcpConfigFile").click());
  $("mcpConfigFile").addEventListener("change", async event => {
    const file = event.target.files[0];
    if (!file) return;
    try {
      if (file.size > 512 * 1024) throw new Error("Config exceeds 512 KB");
      $("importText").value = await file.text();
      state.mcpDraftDirty = true;
      $("configFeedback").textContent = `${file.name} loaded. Review it, then apply the configuration.`;
      $("configFeedback").dataset.kind = "success";
      $("configFeedback").hidden = false;
    } catch (err) { toast(err.message, "error"); }
    event.target.value = "";
  });
  $("currentConfigBtn").addEventListener("click", () => loadCurrentConfig(true).catch(err => toast(err.message, "error")));
  $("downloadConfigBtn").addEventListener("click", async () => {
    try {
      const current = await api("/mcp/config");
      const url = URL.createObjectURL(new Blob([JSON.stringify(current.config, null, 2)], {type:"application/json"}));
      const link = document.createElement("a"); link.href = url; link.download = "mcp.json"; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      if (current.redacted) toast("Downloaded with credential placeholders. Add secret references before importing on another installation.");
    } catch (err) { toast(err.message, "error"); }
  });
  $("skillUploadBtn").addEventListener("click", () => uploadSkill().catch(err => toast(err.message, "error")));
  $("skillRescanBtn").addEventListener("click", async () => {
    const button = $("skillRescanBtn");
    button.disabled = true;
    try {
      const result = await api("/skills/rescan", { method: "POST" });
      toast(`Rescan: ${result.scanned} skill(s), ${result.removed} removed`);
      await renderSkills();
    } catch (err) { toast(err.message, "error"); }
    finally { button.disabled = false; }
  });
  $("secretSaveBtn").addEventListener("click", saveSecret);
  $("unlimitedTools").addEventListener("change", async (event) => {
    const toggle = event.target;
    const requested = toggle.checked;
    state.runModeSaving = true;
    toggle.disabled = true;
    $("sendBtn").disabled = true;
    try {
      const saved = await api("/settings", {method: "PUT", body: JSON.stringify({unlimited_tools: requested})});
      toggle.checked = saved.unlimited_tools;
      announce(requested ? "Unlimited steps enabled for approved tools." : "Standard step limits restored.");
    } catch (err) {
      toggle.checked = !requested;
      toast(err.message, "error");
    } finally { state.runModeSaving = false; toggle.disabled = state.running; $("sendBtn").disabled = state.running; }
  });
  $("promptSaveBtn").addEventListener("click", async () => {
    const button = $("promptSaveBtn");
    button.disabled = true;
    try {
      await api("/settings", {method:"PUT", body:JSON.stringify({system_prompt:$("promptText").value})});
      announce("System prompt saved.");
      toast("Prompt saved");
    } catch (err) { toast(err.message, "error"); }
    finally { button.disabled = false; }
  });
  const input = $("promptInput");
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      submitMessage().catch((err) => toast(err.message, "error"));
    }
  });
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 200) + "px";
  });
}

async function init() {
  const theme = localStorage.getItem("toolloom.theme") || "light";
  applyTheme(theme);
  const stored = localStorage.getItem("toolloom.motion");
  // The OS preference is honoured by CSS on its own; the toggle forces reduced
  // motion on top of it, so reflect the effective state when nothing is stored.
  const motion =
    stored === "reduced" ||
    (stored === null && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches);
  $("motionToggle").checked = Boolean(motion);
  document.documentElement.dataset.motion = motion ? "reduced" : "full";

  wire();
  renderChips();
  await Promise.all([loadSessions().catch((err) => toast(err.message, "error")), loadModels(), loadStatus(), loadRunMode()]);
  await Promise.all([loadUsage(), refreshInspector()]);
}

document.addEventListener("DOMContentLoaded", () => init().catch(err => toast(err.message, "error")));
