// serve/web/harness.js — the Harness tab (strata-harness). Vanilla, no build step
// (ADR-0005); loaded on first use by app.js's loadHarness(). Reuses app.js's
// globals ($, icon, esc, fmt, headers, store, toast) and Strata's design system
// classes only — tool cards use the Chat tab's .tool-call markup, so both tabs
// render a tool call the same way. The transcript is the session event log
// (ADR-0004): durable rows arrive over SSE with their seq; synthetic rows
// (approval/ask, question/ask, inbox/queued) carry seq -1 and reconcile through
// GET /harness/events' pending list.
"use strict";

const H = {
  sid: null,             // the session shown now
  abort: null,           // the live stream's AbortController
  lastSeq: -1,           // highest durable seq painted (dedup + gap detect)
  busy: false,
  sessions: [],
  reconciling: false,
  buffer: [],            // frames that land mid-reconcile (painted after the rebuild)
  seenAsks: new Set(),   // synthetic ask rows already painted (dedup on reconnect)
  retryDelay: 1000,
};

const MAX_RENDERED = 400;   // DOM cap: a long session prunes its oldest rows

// ------------------------------------------------------------------ fetch helpers
async function hget(path) {
  const r = await fetch(path, {headers: headers()});
  if (!r.ok) throw new Error(`${r.status} ${path}`);
  return r.json();
}
async function hpost(path, body) {
  const r = await fetch(path, {
    method: "POST", headers: headers(true), body: JSON.stringify(body || {})});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error((j.error && j.error.message) || `${r.status} ${path}`);
  return j;
}

// ------------------------------------------------------------------ sessions
async function loadSessions() {
  try {
    H.sessions = (await hget("harness/sessions")).sessions;
  } catch (e) { toast("error", "Harness", e.message); return; }
  const box = $("h-session-list");
  box.innerHTML = "";
  for (const s of H.sessions) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "harness-session" + (s.id === H.sid ? " is-active" : "") + (s.origin === "subagent" ? " is-sub" : "");
    row.dataset.sid = s.id;
    row.title = `${s.id} · ${s.error ? "unreadable" : `${s.events} events`}`;
    row.innerHTML = `${icon(s.origin === "subagent" ? "experts" : "tool", "st-icon st-icon--sm")}` +
      `<span>${esc(s.title || s.id)}</span>` +
      (s.error ? `<span class="st-badge st-badge--error">unreadable</span>` : "") +
      (s.origin === "subagent" ? `<span class="st-badge st-badge--reading" title="subagent of ${esc(s.parent || "")}">sub</span>` : "") +
      (s.preset && s.preset !== "default" ? `<span class="st-badge st-badge--reading" title="preset">${esc(s.preset)}</span>` : "") +
      (s.busy ? `<span class="st-badge st-badge--generating">running</span>` : "");
    row.onclick = () => openSession(s.id);
    box.appendChild(row);
  }
  if (!H.sessions.length) box.innerHTML = `<p class="muted small">No sessions yet — start one.</p>`;
}

// keyboard nav for the session list (M4 UX): arrows move focus, Enter opens
function sessionListKeys(e) {
  const rows = [...document.querySelectorAll("#h-session-list .harness-session")];
  if (!rows.length) return;
  const idx = rows.findIndex((r) => r === document.activeElement);
  let next = null;
  if (e.key === "ArrowDown") next = rows[Math.min(idx + 1, rows.length - 1)];
  else if (e.key === "ArrowUp") next = rows[Math.max(idx - 1, 0)];
  else if (e.key === "Home") next = rows[0];
  else if (e.key === "End") next = rows[rows.length - 1];
  if (next) { e.preventDefault(); next.focus(); }
}
$("h-session-list").addEventListener("keydown", sessionListKeys);

$("h-new").onclick = async () => {
  try {
    const {id} = await hpost("harness/session", {preset: $("h-preset").value || "default"});
    store.set("harness.session", id);
    await loadSessions();
    openSession(id);
  } catch (e) { toast("error", "Could not start a session", e.message); }
};

// ------------------------------------------------------------------ presets + export
async function loadPresets() {
  try {
    const {presets} = await hget("harness/presets");
    const sel = $("h-preset");
    sel.innerHTML = "";
    for (const p of presets) {
      const opt = document.createElement("option");
      opt.value = p.name;
      opt.textContent = p.name;
      if (p.persona) opt.title = p.persona;
      sel.appendChild(opt);
    }
    const saved = store.get("harness.preset", "default");
    if (presets.some((p) => p.name === saved)) sel.value = saved;
  } catch { /* the select stays empty: sessions still start with default */ }
}
$("h-preset").onchange = () => store.set("harness.preset", $("h-preset").value);

function exportPopover() {
  closeExportPopover();
  const pop = document.createElement("div");
  pop.id = "h-export-pop";
  pop.className = "st-card harness-popover";
  pop.setAttribute("role", "dialog");
  pop.setAttribute("aria-label", "Export session");
  pop.innerHTML = `<label class="muted small">Download this session</label>` +
    `<div class="harness-popover__actions">` +
    `<button class="st-btn st-btn--primary" id="h-export-md">Markdown</button>` +
    `<button class="st-btn st-btn--secondary" id="h-export-jsonl">JSONL</button></div>` +
    `<p class="muted small">JSONL includes raw tool output — treat it as sensitive.</p>`;
  $("h-bar").appendChild(pop);
  $("h-export-md").focus();
  $("h-export-md").onclick = () => downloadExport("markdown");
  $("h-export-jsonl").onclick = () => downloadExport("jsonl");
  pop.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeExportPopover(); $("h-export").focus(); } });
}
function closeExportPopover() {
  const pop = document.getElementById("h-export-pop");
  if (pop) pop.remove();
}
function downloadExport(fmt) {
  // fetch (not a bare <a>): the route is API-key gated and <a> sends no header
  fetch(`harness/export?session=${encodeURIComponent(H.sid)}&format=${fmt}`, {headers: headers()})
    .then(async (r) => {
      if (!r.ok) throw new Error(`${r.status}`);
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${H.sid}.${fmt === "jsonl" ? "jsonl" : "md"}`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
    })
    .catch((e) => toast("error", "Export", e.message))
    .finally(closeExportPopover);
}
$("h-export").onclick = () => { if (H.sid) exportPopover(); };

// ------------------------------------------------------------------ the stream
function openSession(sid) {
  H.sid = sid;
  store.set("harness.session", sid);
  composerEnabled(true);   // boot may have disabled it (no sessions yet): opening one must re-enable
  H.lastSeq = -1;
  H.seenAsks = new Set();
  H.buffer = [];
  H.retryDelay = 1000;
  H.plan = false; H.goal = null;
  setPlanChip(false); setGoalChip(null, "");   // no stale chips between sessions
  setBusy(false);
  $("h-transcript").innerHTML = `<p class="muted">Loading session ${esc(sid)}&hellip;</p>`;
  loadSessions();
  startStream();
}

function startStream() {
  if (H.abort) H.abort.abort();
  const ctrl = new AbortController();
  H.abort = ctrl;
  fetch(`harness/stream?session=${encodeURIComponent(H.sid)}`, {headers: headers(), signal: ctrl.signal})
    .then(async (r) => {
      if (!r.ok || !r.body) { streamError(`${r.status}`); return; }
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      // the stream replays the whole log from seq 0: reset the dedup state to match
      $("h-transcript").innerHTML = "";
      H.lastSeq = -1;
      H.seenAsks = new Set();
      H.plan = false; H.goal = null;
      setPlanChip(false); setGoalChip(null, "");
      for (;;) {
        const {value, done} = await reader.read();
        if (done) break;
        buf += dec.decode(value, {stream: true});
        let nl;
        while ((nl = buf.indexOf("\n\n")) >= 0) {
          const frame = buf.slice(0, nl);
          buf = buf.slice(nl + 2);
          const line = frame.split("\n").find((l) => l.startsWith("data: "));
          if (!line) continue;                       // ": keep-alive" comment frames
          let row;
          try { row = JSON.parse(line.slice(6)); } catch { continue; }
          if (row.seq >= 0 && row.seq > H.lastSeq + 1 && H.lastSeq >= 0) { reconcile(); continue; }
          if (H.reconciling) H.buffer.push(row);
          else paint(row);
        }
      }
      streamEnded();                                  // clean EOF: the server closed the stream
    })
    .catch((e) => { if (e.name !== "AbortError") streamEnded(); });
}

function streamEnded() {
  if (!H.sid) return;
  reconcile();                                        // catch up on what we missed
  setTimeout(() => { if (H.abort && !H.abort.signal.aborted) startStream(); }, H.retryDelay);
  H.retryDelay = Math.min(H.retryDelay * 2, 15000);   // backoff until a frame lands
}

function streamError(message) {
  // keep the painted history: an error row with Retry, not a wiped transcript
  const row = el("harness-note", `<span class="muted small">${icon("error", "st-icon st-icon--sm")} the harness stream failed (${esc(message)}). ` +
    `<button class="st-btn st-btn--secondary" id="h-retry">Retry</button></span>`);
  row.querySelector("#h-retry").onclick = () => startStream();
}

async function reconcile() {
  if (H.reconciling || !H.sid) return;
  H.reconciling = true;
  try {
    const {events, pending, busy} = await hget(`harness/events?session=${encodeURIComponent(H.sid)}`);
    const buffered = H.buffer; H.buffer = [];
    $("h-transcript").innerHTML = "";
    H.lastSeq = -1;
    H.seenAsks = new Set();
    for (const row of events) paint(row);
    for (const row of pending) paint(row);            // unanswered asks: redraw their cards
    for (const row of buffered) paint(row);
    H.busy = busy; setBusy(busy);
    H.retryDelay = 1000;
  } catch (e) { toast("warn", "Harness", `reconnect failed: ${e.message}`); /* the next frame or a reload retries */ }
  H.reconciling = false;
}

// ------------------------------------------------------------------ painting
function nearBottom() {
  const s = $("h-transcript");
  return s.scrollHeight - s.scrollTop - s.clientHeight < 120;
}
function scrollDown(stick) {
  const s = $("h-transcript");
  if (stick) s.scrollTop = s.scrollHeight;
}

function el(cls, html) {
  const d = document.createElement("div");
  d.className = cls;
  if (html !== undefined) d.innerHTML = html;
  const box = $("h-transcript");
  const stick = nearBottom();
  box.appendChild(d);
  while (box.children.length > MAX_RENDERED) box.removeChild(box.firstChild);   // DOM cap
  scrollDown(stick);
  return d;
}

function paint(row) {
  const t = row.type, d = row.data || {};
  if (row.seq >= 0) {
    if (row.seq <= H.lastSeq) return;                 // duplicate (reconcile overlap)
    H.lastSeq = row.seq;
  }
  if (t === "user/message") {
    if (d.source === "subagent") {
      subagentCard(d);
    } else {
      el("st-msg st-msg--user", `<div class="st-bubble">${esc(d.content)}</div>`);
    }
    H.busy = true; setBusy(true);
  } else if (t === "assistant/message") {
    if (d.content) el("st-msg st-msg--assistant", `<div class="st-bubble">${markdown(d.content)}</div>`);
    if (d.usage) showUsage(d.usage);
  } else if (t === "tool/call") {
    toolCard(d);
  } else if (t === "tool/result") {
    fillToolCard(d);
  } else if (t === "approval/ask") {
    if (H.seenAsks.has(d.call_id)) return;
    H.seenAsks.add(d.call_id);
    approvalCard(d);
  } else if (t === "approval/asked") {
    // the durable twin of the synthetic ask, appended at decision time: the
    // card is already painted (or the pending list brings it) — skip, no duplicate
  } else if (t === "approval/decided") {
    markDecided(d.call_id, d.outcome);
  } else if (t === "question/ask") {
    if (H.seenAsks.has(d.qid)) return;
    H.seenAsks.add(d.qid);
    questionCard(d);
  } else if (t === "turn/end") {
    H.busy = false; setBusy(false);
    if (d.reason !== "completed")
      el("harness-note", `<span class="muted small">turn ${esc(d.reason)}${d.message ? `: ${esc(String(d.message)).slice(0, 200)}` : ""}</span>`);
  } else if (t === "plan/mode") {
    setPlanChip(!!d.active);
    el("harness-note", `<span class="muted small">${icon("layers", "st-icon st-icon--sm")} plan mode ${d.active ? "on" : "off"}</span>`);
  } else if (t === "goal/change") {
    setGoalChip(d.phase, d.objective);
    el("harness-note", `<span class="muted small">${icon("bolt", "st-icon st-icon--sm")} goal ${esc(d.phase)}: ${esc(String(d.objective || "").slice(0, 120))}</span>`);
  } else if (t === "context/pressure") {
    el("harness-note", `<span class="muted small">${icon("warning", "st-icon st-icon--sm")} context ${Math.round((d.pressure || 0) * 100)}% — near the limit</span>`);
  } else if (t === "session/idle") {
    // synthetic: lands after busy=False on the server — the list's badge is fresh now
    setBusy(false);
    loadSessions();
  } else if (t === "compaction/summary") {
    el("harness-note", `<span class="muted small">${icon("layers", "st-icon st-icon--sm")} earlier context compacted</span>`);
  } else if (t === "inbox/queued") {
    el("harness-note", `<span class="muted small">${icon("send", "st-icon st-icon--sm")} ${d.preset === "steer" ? "steering this turn: " : "queued for the next turn: "}${esc(String(d.content || "").slice(0, 200))}</span>`);
  } else if (t === "subagent/spawn") {
    el("harness-note", `<span class="muted small">${icon("experts", "st-icon st-icon--sm")} subagent ${esc(String(d.child || ""))} started (preset ${esc(String(d.preset || "default"))})</span>`);
    loadSessions();
  } else if (t === "preset/warning") {
    el("harness-note", `<span class="muted small">${icon("warning", "st-icon st-icon--sm")} ${esc(String(d.why || "preset problem"))}</span>`);
  }
  // unknown event types are skipped on purpose (forward compatibility)
}

// a settled subagent's report: a distinct card, not a user bubble (M4 UX)
function subagentCard(d) {
  const content = String(d.content || "");
  const m = content.match(/^\[subagent (\S+) (\w+)\]/);
  const child = m ? m[1] : "";
  const state = m ? m[2] : "settled";
  const badge = state === "completed" ? "st-badge--reading"
    : state === "aborted" || state === "error" || state === "blocked" ? "st-badge--error" : "st-badge--generating";
  const card = el("harness-subagent",
    `<div class="harness-subagent__head">${icon("experts", "st-icon st-icon--sm")}` +
    `<h3 class="card-title">Subagent${child ? ` · <button type=\"button\" class=\"harness-session-link\" data-sid=\"${esc(child)}\" title=\"open the child session\">${esc(child)}</button>` : ""}</h3>` +
    `<span class="st-badge ${badge}">${esc(state)}</span></div>` +
    `<div class="harness-subagent__body">${esc(content.replace(/^\[subagent \S+ \w+\]\n?/, ""))}</div>`);
  const link = card.querySelector(".harness-session-link");
  if (link) link.onclick = (e) => { e.stopPropagation(); openSession(link.dataset.sid); };
}

// tool cards: the Chat tab's .tool-call pattern (st-collapse + badge + pre)
// a one-line preview per tool: what it touches, not the raw JSON dump
function toolPreview(d) {
  const a = d.arguments || {};
  const one = (s) => String(s).replace(/\s+/g, " ").trim().slice(0, 120);
  if (d.name === "shell") return one(a.command || "");
  if (d.name === "read_file" || d.name === "write_file" || d.name === "edit_file")
    return [one(a.path || a.file_path || ""), a.limit ? `first ${a.limit} lines` : "",
            a.content ? `${String(a.content).length} chars` : ""].filter(Boolean).join(" · ");
  if (d.name === "search" || d.name === "search_files") return `${one(a.pattern || a.query || "")} in ${one(a.path || ".")}`;
  const first = Object.entries(a)[0];
  return first ? one(`${first[0]}: ${first[1]}`) : "";
}

function toolCard(d) {
  const card = el("harness-tool-wrap",
    `<details class="st-collapse tool-call" data-state="running" open>` +
    `<summary>${icon("tool", "st-icon st-icon--sm")}` +
    `<span class="tool-call__name" title="${esc(d.name)}">${esc(d.name)}</span>` +
    `<span class="tool-call__preview muted">${esc(toolPreview(d))}</span>` +
    `<span class="st-badge st-badge--generating">running</span>` +
    `${icon("chevron", "st-icon st-icon--sm st-chev")}</summary>` +
    `<div class="st-collapse__body"><div class="tool-call__label">Arguments <span class="muted">&middot; ${esc(argsKind(d))}</span></div>` +
    `<pre class="tool-call__pre">${esc(JSON.stringify(d.arguments, null, 1))}</pre></div></details>`);
  card.dataset.callId = d.call_id;
}

// what kind of payload each section holds (M6 UX: the user asked for a
// content descriptor next to Arguments/Result)
function argsKind(d) {
  const a = d.arguments || {};
  if (d.name === "shell") return "shell command";
  if (d.name === "read_file") return "file path" + (a.limit ? ` + line limit` : "");
  if (d.name === "write_file") return `file path + ${fmt(String(a.content || "").length)} chars`;
  if (d.name === "edit_file") return "path + find/replace pair";
  if (d.name === "search") return "pattern + path";
  const keys = Object.keys(a);
  return keys.length ? `${keys.length} field${keys.length > 1 ? "s" : ""}: ${keys.slice(0, 3).join(", ")}` : "no arguments";
}

function resultKind(text) {
  const t = String(text || "");
  const lines = t ? t.split("\n").length : 0;
  const size = `${fmt(t.length)} chars, ${lines} line${lines === 1 ? "" : "s"}`;
  const head = t.trimStart();
  if (head.startsWith("{") || head.startsWith("[")) {
    try { JSON.parse(head); return `JSON (${size})`; } catch { /* not valid JSON */ }
  }
  if (/^total \d+\n(d|l|c|-)/m.test(t) || /^\s*[dlc-][rwx-]{9}/m.test(t)) return `directory listing (${size})`;
  if (/^\s*[\d.]+\s+\S+$/m.test(t) && lines > 1) return `tabular output (${size})`;
  if (/^diff --git /m.test(t)) return `diff (${size})`;
  return `plain text (${size})`;
}

function fillToolCard(d) {
  const wrap = [...document.querySelectorAll(".harness-tool-wrap")].reverse()
    .find((c) => c.dataset.callId === d.call_id);
  const details = wrap ? wrap.querySelector("details") : null;
  const body = details ? details.querySelector(".st-collapse__body") : null;
  if (!body) return;
  body.insertAdjacentHTML("beforeend",
    `<div class="tool-call__label">${d.is_error ? "Error" : "Result"} <span class="muted">&middot; ${esc(resultKind(d.text))}</span>` +
    `${d.spill ? ` &middot; <span class="st-badge st-badge--reading">spilled</span> ` +
      `<code class="harness-spill-locator" title="copy locator">${esc(d.spill.locator)}</code>` : ""}</div>` +
    `<pre class="tool-call__pre">${esc(d.text)}</pre>`);
  // a result that lands while the reader is at the bottom should stay visible
  const stick = nearBottom();
  if (stick) scrollDown(true);
  if (d.spill) {
    // the summary line stays visible when collapsed: say "spilled" there too
    details.querySelector("summary").insertAdjacentHTML("beforeend",
      ` <span class="st-badge st-badge--reading">spilled</span>`);
    const loc = body.querySelector(".harness-spill-locator");
    loc.onclick = () => {
      navigator.clipboard.writeText(d.spill.locator)
        .then(() => toast("success", "Copied", "paste it to the agent with spill_read"))
        .catch(() => {});
    };
  }
  const stateBadge = details.querySelector("summary .st-badge");
  details.dataset.state = d.is_error ? "error" : "done";
  stateBadge.className = "st-badge " + (d.is_error ? "st-badge--error" : "st-badge--reading");
  stateBadge.textContent = d.is_error ? "error" : "done";
  stateBadge.title = resultKind(d.text);   // D4: the collapsed row carries the size in the badge tooltip
  if (d.is_error) details.open = true;
}

function setBusy(busy) {
  H.busy = busy;
  const pill = $("h-state");
  pill.dataset.state = busy ? "generating" : "idle";
  $("h-state-text").textContent = busy ? "working" : "idle";
  // a live caret at the end of the transcript while the model works (M6 UX)
  const box = $("h-transcript");
  const caret = box.querySelector(".harness-typing");
  if (busy && !caret) {
    const d = document.createElement("div");
    d.className = "harness-typing muted small";
    d.textContent = "working";
    d.setAttribute("aria-hidden", "true");   // the state pill already announces busy (D6 a11y)
    box.appendChild(d);
    scrollDown(true);
  } else if (!busy && caret) caret.remove();
  $("h-cancel").hidden = !busy;
  // stop cascades to running subagents (ADR-0008): say so when they exist
  const kids = H.sessions.filter((s) => s.parent === H.sid && s.busy).length;
  $("h-cancel").title = kids ? `Stop the current turn (and ${kids} running subagent${kids > 1 ? "s" : ""})`
                             : "Stop the current turn";
  // steering must be legible before the send, not after (UX review)
  if (!$("h-input").disabled)
    $("h-input").placeholder = busy ? "working — Enter steers this turn" : "Give the agent a task…";
}

function showUsage(u) {
  const max = (lastMetrics && lastMetrics.engine && lastMetrics.engine.max_context) || 0;
  const used = u.prompt_tokens || 0;                  // what the last request actually held
  const pct = max ? Math.round((used / max) * 100) : 0;
  const warn = max && pct > 85;
  $("h-ctx").innerHTML = max
    ? `${warn ? `${icon("warning", "st-icon st-icon--sm")} near the limit: ` : ""}context ${pct}% (${fmt(used)}/${fmt(max)})`
    : `prompt ${fmt(used)} tokens`;
  // honesty (M6): the denominator is THIS server's engine window; when the
  // harness runs beside a different model (HTTP-attached), say which it is
  $("h-ctx").title = max ? `of this server's engine context window (${fmt(max)} tokens)` : "";
  $("h-ctx").classList.toggle("harness-ctx--warn", warn);
}

// ------------------------------------------------------------------ approvals + questions
function approvalCard(d) {
  const card = el("harness-approval",
    `<div class="harness-approval__head">${icon("warning", "st-icon st-icon--sm")}` +
    `<span class="card-title">Run <code class="inline">${esc(d.tool)}</code>?${d.session ? ` <span class="st-badge st-badge--generating" title="asked by subagent ${esc(d.session)}">subagent</span>` : ""}</span></div>` +
    `<pre class="st-code">${esc(d.preview || "")}</pre>` +
    `<div class="harness-approval__actions">` +
    `<button class="st-btn st-btn--primary" data-a="allow-once">Allow once</button>` +
    `<button class="st-btn st-btn--danger" data-a="deny">Deny</button></div>`);
  card.dataset.callId = d.call_id;
  card.querySelectorAll("[data-a]").forEach((b) => {
    b.onclick = async () => {
      try {
        await hpost(`harness/session/${H.sid}/decide`, {call_id: d.call_id, outcome: b.dataset.a});
        card.querySelectorAll("[data-a]").forEach((x) => { x.disabled = true; });
      } catch (e) { toast("warn", "Approval", e.message); }
    };
  });
}

function markDecided(callId, outcome) {
  const card = [...document.querySelectorAll(".harness-approval")].find((c) => c.dataset.callId === callId);
  if (!card) return;
  // the decision is made: fold the card to one quiet line (M6 UX: a decided
  // approval must not compete with the live step; the preview stays available)
  const preview = card.querySelector("pre")?.textContent || "";
  const head = card.querySelector(".harness-approval__head");
  const title = head.querySelector(".card-title")?.textContent || "";
  card.dataset.decided = outcome;
  card.innerHTML =
    `<details class="harness-approval__fold">` +
    `<summary>${icon(outcome === "deny" ? "error" : "check", "st-icon st-icon--sm")}` +
    `<span class="tool-call__name">${esc(title.trim())}</span>` +
    `<span class="tool-call__preview muted">${esc(preview.split("\n")[0].slice(0, 90))}</span>` +
    `<span class="st-badge ${outcome === "deny" ? "st-badge--error" : "st-badge--reading"}">${esc(outcome)}</span>` +
    `${icon("chevron", "st-icon st-icon--sm st-chev")}</summary>` +
    `<div class="st-collapse__body"><pre class="tool-call__pre">${esc(preview)}</pre></div></details>`;
}

function questionCard(d) {
  const card = el("harness-question",
    `<div class="harness-question__head">${icon("info", "st-icon st-icon--sm")}<span class="card-title">${esc(d.question)}</span></div>`);
  const row = document.createElement("div");
  row.className = "harness-question__actions";
  card.appendChild(row);
  const chosen = new Set();
  const send = async (answer) => {
    try {
      await hpost(`harness/session/${H.sid}/answer`, {qid: d.qid, answer});
      card.querySelectorAll("button").forEach((x) => { x.disabled = true; });
    } catch (e) { toast("warn", "Answer", e.message); }
  };
  for (const opt of d.options || []) {
    const b = document.createElement("button");
    b.className = "st-btn st-btn--secondary";
    b.textContent = opt.label || "";
    if (d.multi) {
      b.onclick = () => {                              // multi-select: toggle, then Send
        if (chosen.has(opt.label)) { chosen.delete(opt.label); b.classList.remove("is-on"); }
        else { chosen.add(opt.label); b.classList.add("is-on"); }
      };
    } else {
      b.onclick = () => send({selected: opt.label || ""});
    }
    row.appendChild(b);
  }
  if (d.multi && (d.options || []).length) {
    const b = document.createElement("button");
    b.className = "st-btn st-btn--primary";
    b.textContent = "Send";
    b.onclick = () => send({selected: [...chosen]});
    row.appendChild(b);
  }
  const input = document.createElement("input");
  input.className = "st-input";
  input.placeholder = "answer…";
  input.setAttribute("aria-label", "Answer");
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); send({text: input.value}); } });
  const b = document.createElement("button");
  b.className = "st-btn st-btn--primary";
  b.textContent = "Send";
  b.onclick = () => send({text: input.value});
  row.append(input, b);
}

// ------------------------------------------------------------------ composer + controls
function composerEnabled(on) {
  $("h-input").disabled = !on;
  $("h-input").placeholder = on ? "Give the agent a task…" : "Start or open a session first";
  // the send button must not glow green when the composer is disabled (UX review)
  document.querySelector("#h-composer button[type=submit]").disabled = !on;
  // the session state pill only means something with a session open: the header
  // pill already shows the server state, don't duplicate it in the empty tab
  $("h-state").hidden = !on && !H.busy;
  // the whole session-scoped bar is honest about the empty state (UX review)
  for (const id of ["h-plan", "h-goal", "h-fork", "h-policy", "h-allowlist", "h-export"]) $(id).disabled = !on;
}

// plan / goal mode chips + buttons (ADR-0007). State is tracked in H (not read
// back from the DOM) so a slow SSE echo can't race a click.
function setPlanChip(on) {
  H.plan = on;
  const b = $("h-plan");
  b.classList.toggle("is-on", on);
  b.setAttribute("aria-pressed", on ? "true" : "false");
  b.setAttribute("aria-label", on ? "Plan mode on — click to leave" : "Plan mode off — click to enter");
}
function setGoalChip(phase, objective) {
  H.goal = phase ? {phase, objective: objective || ""} : null;
  const b = $("h-goal");
  b.classList.toggle("is-on", phase === "active");
  b.setAttribute("aria-pressed", phase === "active" ? "true" : "false");
  const label = phase ? `Goal ${phase}${objective ? `: ${String(objective).slice(0, 80)}` : ""}`
                      : "Set a goal";
  b.textContent = phase ? `Goal · ${phase}` : "Goal";
  b.setAttribute("aria-label", label);
  b.title = label;
}
$("h-plan").onclick = async () => {
  if (!H.sid) return;
  try { await hpost(`harness/session/${H.sid}/mode`, {plan: !H.plan}); }
  catch (e) { toast("error", "Plan mode", e.message); }
};

// goal popover: an inline st-input instead of a native prompt() (UX review:
// off-system, inaccessible). Enter submits, Esc closes, buttons for mouse.
function goalPopover() {
  closeGoalPopover();
  const pop = document.createElement("div");
  pop.id = "h-goal-pop";
  pop.className = "st-card harness-popover";
  pop.setAttribute("role", "dialog");
  pop.setAttribute("aria-label", "Set a goal");
  pop.innerHTML = `<label class="muted small" for="h-goal-input">Objective (the agent keeps working toward it each turn)</label>` +
    `<input class="st-input" id="h-goal-input" placeholder="ship the harness demo…" aria-label="Goal objective">` +
    `<div class="harness-popover__actions">` +
    `<button class="st-btn st-btn--primary" id="h-goal-save">Start goal</button>` +
    `<button class="st-btn st-btn--secondary" id="h-goal-pause">Pause active goal</button>` +
    `<button class="st-btn st-btn--icon" id="h-goal-close" title="Close" aria-label="Close">✕</button></div>`;
  $("h-bar").appendChild(pop);
  const input = $("h-goal-input");
  input.focus();
  const submit = async (body) => {
    try { await hpost(`harness/session/${H.sid}/mode`, body); closeGoalPopover(); }
    catch (e) { toast("error", "Goal mode", e.message); }
  };
  $("h-goal-save").onclick = () => {
    const objective = input.value.trim();
    if (!objective) { input.focus(); return; }
    submit({goal: {objective, phase: "active", maxRounds: 8}});
  };
  $("h-goal-pause").onclick = () => submit({goal: {objective: (H.goal && H.goal.objective) || "", phase: "paused"}});
  $("h-goal-close").onclick = closeGoalPopover;
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); $("h-goal-save").click(); }
    if (e.key === "Escape") { closeGoalPopover(); $("h-goal").focus(); }
  });
}
function closeGoalPopover() {
  const pop = document.getElementById("h-goal-pop");
  if (pop) pop.remove();
}
$("h-goal").onclick = () => { if (H.sid) goalPopover(); };

// "/" focuses the composer from the transcript (dsh-style; M4 UX)
document.addEventListener("keydown", (e) => {
  if (e.key !== "/" || e.isComposing) return;
  const t = e.target;
  if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable)) return;
  const view = document.getElementById("view-harness");
  if (!view || view.hidden) return;
  if ($("h-input").disabled) return;
  e.preventDefault();
  $("h-input").focus();
});

// composer: idle -> new turn; busy -> steer the running turn (dsh semantics)
// parity with the Chat composer: the textarea grows with the text (app.js autosize)
function autosizeHarness() {
  const t = $("h-input");
  t.style.height = "auto";
  t.style.height = `${Math.min(t.scrollHeight, innerHeight * 0.4)}px`;
}
$("h-input").addEventListener("input", autosizeHarness);
$("h-composer").onsubmit = async (e) => {
  e.preventDefault();
  const text = $("h-input").value.trim();
  if (!text) return;
  if (!H.sid) { toast("info", "No session", "Start one with the + button."); return; }
  $("h-input").value = "";
  autosizeHarness();
  try {
    const body = {content: text, target: H.busy ? "steer" : "auto"};
    await hpost(`harness/session/${H.sid}/message`, body);
  } catch (err) { toast("error", "Send", err.message); }
};
// parity with the Chat composer: Enter sends, Shift+Enter is a new line
$("h-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("h-composer").requestSubmit(); }
});

// code blocks rendered by markdown() get the Chat tab's copy button behaviour
$("h-transcript").addEventListener("click", (e) => {
  const cc = e.target.closest("[data-code-copy]");
  if (cc) copyText(cc.closest(".st-code").querySelector("pre").textContent, cc);
});

$("h-cancel").onclick = async () => {
  try { await hpost(`harness/session/${H.sid}/cancel`, {}); } catch (e) { toast("warn", "Cancel", e.message); }
};

$("h-fork").onclick = async () => {
  if (!H.sid) return;
  try {
    const {id} = await hpost(`harness/session/${H.sid}/fork`, {});
    toast("success", "Forked", `new session ${id}`);
    await loadSessions();
    openSession(id);
  } catch (e) { toast("error", "Fork", e.message); }
};

// approval policy: ask | never | allowlist (patterns ride the store)
$("h-policy").onchange = () => {
  $("h-allowlist").hidden = $("h-policy").value !== "allowlist";
  savePolicy();
};
$("h-allowlist").onchange = savePolicy;
async function savePolicy() {
  if (!H.sid) return;
  const pats = $("h-allowlist").value.split(",").map((p) => p.trim()).filter(Boolean);
  store.set("harness.allowlist", pats);
  try {
    await hpost(`harness/session/${H.sid}/policy`, {mode: $("h-policy").value, allowlist: pats});
    toast("success", "Approval policy", $("h-policy").value);
  } catch (e) { toast("error", "Policy", e.message); }
}

// ------------------------------------------------------------------ start
(async function harnessBoot() {
  $("h-allowlist").value = store.get("harness.allowlist", []).join(", ");
  await loadPresets();
  await loadSessions();
  const saved = store.get("harness.session", null);
  if (saved && H.sessions.some((s) => s.id === saved)) openSession(saved);
  else if (H.sessions.length) openSession(H.sessions[0].id);
  else composerEnabled(false);
})();
