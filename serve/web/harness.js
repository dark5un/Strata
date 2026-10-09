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
    row.className = "harness-session" + (s.id === H.sid ? " is-active" : "");
    row.innerHTML = `${icon("tool", "st-icon st-icon--sm")}<span>${esc(s.id)}</span>` +
      `<span class="muted small">${s.error ? "unreadable" : `${s.events} events`}</span>` +
      (s.busy ? `<span class="st-badge st-badge--generating">running</span>` : "");
    row.onclick = () => openSession(s.id);
    box.appendChild(row);
  }
  if (!H.sessions.length) box.innerHTML = `<p class="muted small">No sessions yet — start one.</p>`;
}

$("h-new").onclick = async () => {
  try {
    const {id} = await hpost("harness/session", {});
    store.set("harness.session", id);
    await loadSessions();
    openSession(id);
  } catch (e) { toast("error", "Could not start a session", e.message); }
};

// ------------------------------------------------------------------ the stream
function openSession(sid) {
  H.sid = sid;
  store.set("harness.session", sid);
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
    el("st-msg st-msg--user", `<div class="st-bubble">${esc(d.content)}</div>`);
    H.busy = true; setBusy(true);
  } else if (t === "assistant/message") {
    if (d.content) el("st-msg st-msg--assistant", `<div class="st-bubble">${esc(d.content)}</div>`);
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
  }
  // unknown event types are skipped on purpose (forward compatibility)
}

// tool cards: the Chat tab's .tool-call pattern (st-collapse + badge + pre)
function toolCard(d) {
  const card = el("harness-tool-wrap",
    `<details class="st-collapse tool-call" data-state="running" open>` +
    `<summary>${icon("tool", "st-icon st-icon--sm")}` +
    `<span class="tool-call__name" title="${esc(d.name)}">${esc(d.name)}</span>` +
    `<span class="tool-call__preview muted">${esc(JSON.stringify(d.arguments)).slice(0, 200)}</span>` +
    `<span class="st-badge st-badge--generating">running</span>` +
    `${icon("chevron", "st-icon st-icon--sm st-chev")}</summary>` +
    `<div class="st-collapse__body"><div class="tool-call__label">Arguments</div>` +
    `<pre class="tool-call__pre">${esc(JSON.stringify(d.arguments, null, 1))}</pre></div></details>`);
  card.dataset.callId = d.call_id;
}

function fillToolCard(d) {
  const wrap = [...document.querySelectorAll(".harness-tool-wrap")].reverse()
    .find((c) => c.dataset.callId === d.call_id);
  const details = wrap ? wrap.querySelector("details") : null;
  const body = details ? details.querySelector(".st-collapse__body") : null;
  if (!body) return;
  body.insertAdjacentHTML("beforeend",
    `<div class="tool-call__label">${d.is_error ? "Error" : "Result"}` +
    `${d.spill ? ` &middot; <span class="st-badge st-badge--reading">spilled</span> ` +
      `<code class="harness-spill-locator" title="copy locator">${esc(d.spill.locator)}</code>` : ""}</div>` +
    `<pre class="tool-call__pre">${esc(d.text)}</pre>`);
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
  if (d.is_error) details.open = true;
}

function setBusy(busy) {
  H.busy = busy;
  const pill = $("h-state");
  pill.dataset.state = busy ? "generating" : "idle";
  $("h-state-text").textContent = busy ? "working" : "idle";
  $("h-cancel").hidden = !busy;
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
  $("h-ctx").classList.toggle("harness-ctx--warn", warn);
}

// ------------------------------------------------------------------ approvals + questions
function approvalCard(d) {
  const card = el("harness-approval",
    `<div class="harness-approval__head">${icon("warning", "st-icon st-icon--sm")}<span class="card-title">approve ${esc(d.tool)}?</span></div>` +
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
  card.querySelectorAll("[data-a]").forEach((x) => { x.disabled = true; });
  const s = document.createElement("span");
  s.className = "muted small";
  s.textContent = ` ${outcome}`;
  card.querySelector(".harness-approval__head").appendChild(s);
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
  // the whole session-scoped bar is honest about the empty state (UX review)
  for (const id of ["h-plan", "h-goal", "h-fork", "h-policy", "h-allowlist"]) $(id).disabled = !on;
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
    if (e.key === "Escape") closeGoalPopover();
  });
}
function closeGoalPopover() {
  const pop = document.getElementById("h-goal-pop");
  if (pop) pop.remove();
}
$("h-goal").onclick = () => { if (H.sid) goalPopover(); };

// composer: idle -> new turn; busy -> steer the running turn (dsh semantics)
$("h-composer").onsubmit = async (e) => {
  e.preventDefault();
  const text = $("h-input").value.trim();
  if (!text) return;
  if (!H.sid) { toast("info", "No session", "Start one with the + button."); return; }
  $("h-input").value = "";
  try {
    const body = {content: text, target: H.busy ? "steer" : "auto"};
    await hpost(`harness/session/${H.sid}/message`, body);
  } catch (err) { toast("error", "Send", err.message); }
};
// parity with the Chat composer: Enter sends, Shift+Enter is a new line
$("h-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("h-composer").requestSubmit(); }
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
  await loadSessions();
  const saved = store.get("harness.session", null);
  if (saved && H.sessions.some((s) => s.id === saved)) openSession(saved);
  else if (H.sessions.length) openSession(H.sessions[0].id);
  else composerEnabled(false);
})();
