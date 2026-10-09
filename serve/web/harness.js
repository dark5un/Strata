// serve/web/harness.js — the Harness tab (strata-harness). Vanilla, no build step
// (ADR-0005); loaded on first use by app.js's loadHarness(). Reuses app.js's
// globals ($, icon, esc, headers, store, toast) and Strata's design system
// classes only. The transcript is the session event log (ADR-0004): durable
// rows arrive over SSE with their seq; synthetic rows (approval/ask,
// question/ask, inbox/queued) carry seq -1 and reconcile on reconnect.
"use strict";

const H = {
  sid: null,            // the session shown now
  es: null,             // the SSE reader's abort controller
  lastSeq: -1,          // highest durable seq seen (gap detection)
  busy: false,
  sessions: [],
};

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
    row.innerHTML = `${icon("chat", "st-icon st-icon--sm")}<span>${esc(s.id)}</span>` +
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
  setBusy(false);
  $("h-transcript").innerHTML = `<p class="muted">Loading session ${esc(sid)}&hellip;</p>`;
  loadSessions();
  startStream();
}

function startStream() {
  if (H.es) H.es.abort();
  const ctrl = new AbortController();
  H.es = ctrl;
  fetch(`harness/stream?session=${encodeURIComponent(H.sid)}`, {headers: headers(), signal: ctrl.signal})
    .then(async (r) => {
      if (!r.ok || !r.body) { toast("error", "Harness stream", `${r.status}`); return; }
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      $("h-transcript").innerHTML = "";
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
          try { row = JSON.parse(line.slice(6)); } catch (e) { continue; }
          if (row.seq >= 0) {
            if (row.seq > H.lastSeq + 1 && H.lastSeq >= 0) reconcile();   // a gap: refetch the log
            H.lastSeq = Math.max(H.lastSeq, row.seq);
          }
          paint(row);
        }
      }
    })
    .catch((e) => { if (e.name !== "AbortError") toast("warn", "Harness stream ended", e.message, 4000); });
}

async function reconcile() {
  if (!H.sid) return;
  try {
    const {events, busy} = await hget(`harness/events?session=${encodeURIComponent(H.sid)}`);
    $("h-transcript").innerHTML = "";
    H.lastSeq = -1;
    for (const row of events) { H.lastSeq = Math.max(H.lastSeq, row.seq); paint(row, true); }
    H.busy = busy; setBusy(busy);
  } catch (e) { /* the next frame or a reload retries */ }
}

// ------------------------------------------------------------------ painting
function el(cls, html) {
  const d = document.createElement("div");
  d.className = cls;
  if (html !== undefined) d.innerHTML = html;
  $("h-transcript").appendChild(d);
  $("h-transcript").scrollTop = $("h-transcript").scrollHeight;
  return d;
}

function paint(row, quiet) {
  const t = row.type, d = row.data || {};
  if (t === "user/message") {
    el("st-msg st-msg--user", `<div class="st-bubble">${esc(d.content)}</div>`);
    H.busy = true; setBusy(true);
  } else if (t === "assistant/message") {
    if (d.content) el("st-msg st-msg--assistant", `<div class="st-bubble">${esc(d.content)}</div>`);
    if (d.usage) showUsage(d.usage);
  } else if (t === "tool/call") {
    const card = el("harness-tool", `<div class="harness-tool__head">${icon("tool", "st-icon st-icon--sm")}` +
      `<span class="card-title">${esc(d.name)}</span><span class="muted small">${esc(JSON.stringify(d.arguments)).slice(0, 160)}</span></div>` +
      `<div class="harness-tool__body" hidden></div>`);
    card.dataset.callId = d.call_id;
  } else if (t === "tool/result") {
    const card = [...document.querySelectorAll(".harness-tool")].reverse()
      .find((c) => c.dataset.callId === d.call_id);
    const body = card ? card.querySelector(".harness-tool__body") : el("harness-tool").querySelector(".harness-tool__body");
    if (body) {
      body.hidden = false;
      body.innerHTML = `<pre class="st-code${d.is_error ? " harness-tool__error" : ""}">${esc(d.text)}</pre>` +
        (d.spill ? `<p class="muted small">full output: ${esc(d.spill.locator)}</p>` : "");
      if (card) card.querySelector(".harness-tool__head").classList.add(d.is_error ? "is-error" : "is-done");
    }
  } else if (t === "approval/ask") {
    approvalCard(d);
  } else if (t === "approval/decided") {
    markDecided(d.call_id, d.outcome);
  } else if (t === "question/ask") {
    questionCard(d);
  } else if (t === "turn/end") {
    H.busy = false; setBusy(false);
    loadSessions();          // the session list's event count is stale after a turn
    if (d.reason !== "completed") el("harness-note", `<span class="muted small">turn ${esc(d.reason)}${d.message ? `: ${esc(String(d.message)).slice(0, 200)}` : ""}</span>`);
  } else if (t === "compaction/summary") {
    el("harness-note", `<span class="muted small">${icon("layers", "st-icon st-icon--sm")} earlier context compacted</span>`);
  } else if (t === "inbox/queued") {
    el("harness-note", `<span class="muted small">queued for the next turn: ${esc(d.content || "")}</span>`);
  }
  // unknown event types are skipped on purpose (forward compatibility)
  if (!quiet) { /* nothing else to do */ }
}

function setBusy(busy) {
  H.busy = busy;
  const pill = $("h-state");
  pill.dataset.state = busy ? "generating" : "idle";
  $("h-state-text").textContent = busy ? "working" : "idle";
  $("h-cancel").hidden = !busy;
}

function showUsage(u) {
  const max = (lastMetrics && lastMetrics.engine && lastMetrics.engine.max_context) || 0;
  const used = (u.prompt_tokens || 0) + (u.completion_tokens || 0);
  $("h-ctx").textContent = max ? `context ${Math.round((used / max) * 100)}% (${used}/${max})` :
    `tokens ${used}`;
  if (max && used / max > 0.85) $("h-ctx").classList.add("harness-ctx--warn");
  else $("h-ctx").classList.remove("harness-ctx--warn");
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
  const send = async (answer) => {
    try {
      await hpost(`harness/session/${H.sid}/answer`, {qid: d.qid, answer});
      row.querySelectorAll("button").forEach((x) => { x.disabled = true; });
    } catch (e) { toast("warn", "Answer", e.message); }
  };
  for (const opt of d.options || []) {
    const b = document.createElement("button");
    b.className = "st-btn st-btn--secondary";
    b.textContent = opt.label || "";
    b.onclick = () => send({selected: opt.label || ""});
    row.appendChild(b);
  }
  const input = document.createElement("input");
  input.className = "st-input";
  input.placeholder = "answer&hellip;";
  input.setAttribute("aria-label", "Answer");
  const b = document.createElement("button");
  b.className = "st-btn st-btn--primary";
  b.textContent = "Send";
  b.onclick = () => send({text: input.value});
  row.append(input, b);
}

// ------------------------------------------------------------------ composer + controls
$("h-composer").onsubmit = async (e) => {
  e.preventDefault();
  const text = $("h-input").value.trim();
  if (!text || !H.sid) return;
  $("h-input").value = "";
  try {
    await hpost(`harness/session/${H.sid}/message`, {content: text});
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

$("h-policy").onchange = async () => {
  if (!H.sid) return;
  try {
    await hpost(`harness/session/${H.sid}/policy`, {mode: $("h-policy").value, allowlist: store.get("harness.allowlist", [])});
    toast("success", "Approval policy", $("h-policy").value);
  } catch (e) { toast("error", "Policy", e.message); }
};

// ------------------------------------------------------------------ start
(async function harnessBoot() {
  await loadSessions();
  const saved = store.get("harness.session", null);
  if (saved && H.sessions.some((s) => s.id === saved)) openSession(saved);
  else if (H.sessions.length) openSession(H.sessions[0].id);
})();
