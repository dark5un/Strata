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
  ctxEstimate: 0,        // the meter's fold (stream/delta kind=context): the pill's source of truth
  roots: [],             // M9: the open session's trusted roots (from the list row)
  pendingRoots: [],      // M9: the picker's roots for the NEXT session (defaults to the last one's)
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

// ------------------------------------------------------------------ popover stacking (UX review)
// A popover must visibly OWN the screen: everything behind it dims, and a
// dialog opened over a dialog dims the dialog below it. Focus is shown, not
// implied. mountPopover arms a scrim under each popover and tracks depth;
// unmountPopover retires the scrim when its popover closes.
let POP_STACK = 0;
function mountPopover(pop) {
  POP_STACK += 1;
  pop.dataset.stack = String(POP_STACK);
  pop.style.zIndex = String(29 + POP_STACK * 2);
  const scrim = document.createElement("div");
  scrim.className = "harness-scrim";
  scrim.dataset.stack = String(POP_STACK);
  scrim.style.zIndex = String(28 + POP_STACK * 2);
  scrim.onclick = () => closeTopPopover();
  $("h-bar").appendChild(scrim);
  $("h-bar").appendChild(pop);   // the scrim first, the popover above it
  return pop;
}
function unmountPopover(pop) {
  const stack = parseInt(pop.dataset.stack || "0", 10);
  if (!stack) return;
  document.querySelectorAll(`.harness-scrim[data-stack="${stack}"]`).forEach((s) => s.remove());
  if (POP_STACK >= stack) POP_STACK = stack - 1;
}
// close the topmost popover (scrim click / global Escape): find the live pop
// with the highest stack and run its own close path via a synthetic Escape,
// so each popover keeps its focus-return behaviour.
function closeTopPopover() {
  const pops = [...document.querySelectorAll("#h-bar [data-stack]")];
  if (!pops.length) return;
  pops.sort((a, b) => (parseInt(a.dataset.stack, 10) - parseInt(b.dataset.stack, 10)));
  const top = pops[pops.length - 1];
  // the popover's own Escape path runs on this synthetic event and bubbles
  // back here; the flag stops the document listener from cascading the close
  // into the popover BELOW (one scrim click = one popover closes)
  closingTop = true;
  try { top.dispatchEvent(new window.KeyboardEvent("keydown", {key: "Escape", bubbles: true})); }
  finally { closingTop = false; }
}
let closingTop = false;
document.addEventListener("keydown", (e) => {
  if (closingTop) return;
  if (e.key === "Escape" && document.querySelector("#h-bar .harness-scrim")) {
    const focused = document.activeElement;
    if (focused && focused.closest && focused.closest("#h-bar [data-stack]")) return;  // popover handles its own
    closeTopPopover();
  }
});

// ------------------------------------------------------------------ sessions
async function loadSessions() {
  try {
    const d = await hget("harness/sessions");
    H.sessions = d.sessions;
    // M9: the server's default trusted folders seed the picker on boot, so
    // a fresh page shows the fence a no-roots session would actually get
    // (the deployment sets STRATA_HARNESS_ROOTS; the server applies it too).
    if (d.defaultRoots && !H.pendingRoots.length && !H.sid) {
      H.pendingRoots = [...d.defaultRoots];
      setWsChip();
    }
  } catch (e) { toast("error", "Harness", e.message); return; }
  const box = $("h-session-list");
  box.innerHTML = "";
  for (const s of H.sessions) {
    // role=button div (not <button>): the row hosts a nested delete button,
    // which invalid-HTML would otherwise swallow (M10)
    const row = document.createElement("div");
    row.setAttribute("role", "button");
    row.tabIndex = 0;
    row.className = "harness-session" + (s.id === H.sid ? " is-active" : "") + (s.origin === "subagent" ? " is-sub" : "");
    row.dataset.sid = s.id;
    row.title = `${s.id} · ${s.error ? "unreadable" : `${s.events} events`}`;
    row.innerHTML = `${icon(s.origin === "subagent" ? "experts" : "tool", "st-icon st-icon--sm")}` +
      `<span>${esc(s.title || s.id)}</span>` +
      (s.error ? `<span class="st-badge st-badge--error">unreadable</span>` : "") +
      (s.origin === "subagent" ? `<span class="st-badge st-badge--reading" title="subagent of ${esc(s.parent || "")}">sub</span>` : "") +
      (s.preset && s.preset !== "default" ? `<span class="st-badge st-badge--reading" title="preset">${esc(s.preset)}</span>` : "") +
      (s.roots && s.roots.length ? `<span class="st-badge st-badge--reading harness-ws-badge" title="${esc(s.roots.join("\n"))}">${esc(wsShortName(s.roots[0]))}${s.roots.length > 1 ? ` +${s.roots.length - 1}` : ""}</span>` : "") +
      (s.busy ? `<span class="st-badge st-badge--generating">running</span>` : "") +
      `<button class="st-btn st-btn--icon harness-session-del" title="Delete session" aria-label="Delete ${esc(s.title || s.id)}">` +
      `${icon("trash", "st-icon st-icon--sm")}</button>`;
    row.onclick = () => openSession(s.id);
    row.onkeydown = (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openSession(s.id); }
    };
    const del = row.querySelector(".harness-session-del");
    del.onclick = (e) => { e.stopPropagation(); confirmDeleteSession(row, del, s); };
    box.appendChild(row);
  }
  if (!H.sessions.length) box.innerHTML = `<p class="muted small">No sessions yet — start one.</p>`;
}

// session delete (M10): destructive, so two-step inline confirm — the trash
// becomes Delete/Keep in place. No native confirm() (off-system, the goal
// popover set the precedent). The log IS the session (ADR-0004): deleting
// removes the JSONL; children keep their logs and just lose the pointer.
function confirmDeleteSession(row, del, s) {
  if (row.dataset.confirming) return;
  row.dataset.confirming = "1";
  row.classList.add("is-confirming");
  del.hidden = true;
  const acts = document.createElement("span");
  acts.className = "harness-session-del-acts";
  acts.innerHTML = `<button class="st-btn st-btn--danger harness-session-yes">Delete</button>` +
    `<button class="st-btn st-btn--secondary harness-session-keep">Keep</button>`;
  row.appendChild(acts);
  const yes = acts.querySelector(".harness-session-yes");
  yes.focus();
  const undo = () => { delete row.dataset.confirming; row.classList.remove("is-confirming"); acts.remove(); del.hidden = false; };
  acts.querySelector(".harness-session-keep").onclick = (e) => { e.stopPropagation(); undo(); };
  row.addEventListener("mouseleave", () => { if (row.dataset.confirming && !row.contains(document.activeElement)) undo(); });
  yes.onclick = async (e) => {
    e.stopPropagation();
    try {
      await hpost(`harness/session/${s.id}/delete`, {});
      toast("success", "Session deleted", s.title || s.id);
      if (H.sid === s.id) {
        H.sid = null;
        if (H.abort) H.abort.abort();
        $("h-transcript").innerHTML = "";
        composerEnabled(false);
        store.set("harness.session", null);
      }
      await loadSessions();
      if (H.sessions.length && !H.sid) openSession(H.sessions[0].id);
    } catch (err) { toast("error", "Delete session", err.message); undo(); }
  };
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

async function startSession() {
  try {
    // M9: the picker's pending roots ride the create call; they lock into the
    // session header at creation (the first message can no longer change them).
    const body = {preset: $("h-preset").value || "default"};
    if (H.pendingRoots.length) body.roots = H.pendingRoots;
    const {id} = await hpost("harness/session", body);
    store.set("harness.session", id);
    await loadSessions();
    openSession(id);
  } catch (e) { toast("error", "Could not start a session", e.message); }
}
$("h-new").onclick = startSession;

// ------------------------------------------------------------------ workspaces (M9 / ADR-0015)
// The session bar's workspace chip + roots popover. Folders are just folders:
// the picker autocompletes against the container's tree via harness/ls
// (directories only). Roots lock at session creation; a live session's roots
// are read-only, and the popover's escape hatch is the fork-with-roots picker
// below — the copy must always name a control that actually exists (UX review
// of the M9 screenshot: "fork to change" was a dead end because the Fork
// button posted an empty body, so forking could never change roots).
function wsShortName(p) {
  const parts = String(p).replace(/\/+$/, "").split("/");
  return parts[parts.length - 1] || p;
}

function setWsChip() {
  const chip = $("h-ws"), label = $("h-ws-label"), count = $("h-ws-count"), lock = $("h-ws-lock");
  // with no session open the chip previews the picker's pending roots
  const roots = H.sid ? (H.roots || []) : H.pendingRoots;
  // honesty (UX review): a live session's roots are frozen — say so on the
  // chip itself, not only in the popover's small print
  if (lock) lock.hidden = !H.sid;
  if (!roots.length) {
    label.textContent = "workspace: server default";
    count.hidden = true;
    chip.title = "No trusted folders set — the session uses the server's default workspace";
  } else {
    label.textContent = wsShortName(roots[0]);
    count.hidden = roots.length < 2;
    if (roots.length >= 2) count.textContent = `+${roots.length - 1}`;
    chip.title = roots.join("\n");
  }
  // the chip stays enabled with no session open: it arms the picker for the
  // NEXT session (the design's "last session's roots as the default")
}

function closeWsPop() {
  const pop = document.getElementById("h-ws-pop");
  if (pop) { unmountPopover(pop); pop.remove(); }
  $("h-ws").setAttribute("aria-expanded", "false");
}

// shared autocomplete: typing a path browses the container's tree via
// harness/ls (directories only). Enter-to-add stays with the caller because
// the target list differs (pending roots vs fork roots).
function wireRootsBrowse(input, suggest) {
  let timer = null;
  const browse = async (frag) => {
    try {
      const lastSlash = frag.lastIndexOf("/");
      const parent = lastSlash > 0 ? frag.slice(0, lastSlash) : "/";
      const needle = frag.slice(lastSlash + 1).toLowerCase();
      const {dirs} = await hget(`harness/ls?path=${encodeURIComponent(parent)}`);
      const hits = dirs.filter((d) => d.name.toLowerCase().includes(needle)).slice(0, 12);
      suggest.innerHTML = "";
      for (const d of hits) {
        const b = document.createElement("button");
        b.type = "button"; b.className = "harness-ws-hit"; b.setAttribute("role", "option");
        b.textContent = d.path;
        b.onclick = () => { input.value = d.path + "/"; suggest.hidden = true; input.focus(); };
        suggest.appendChild(b);
      }
      suggest.hidden = !hits.length;
    } catch { suggest.hidden = true; }
  };
  input.oninput = () => { clearTimeout(timer); timer = setTimeout(() => browse(input.value), 250); };
  input.onkeydown = (e) => {
    if (e.key === "ArrowDown") {
      const first = suggest.querySelector("button"); if (first) { e.preventDefault(); first.focus(); }
    }
  };
  suggest.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      const items = [...suggest.querySelectorAll("button")];
      const i = items.findIndex((x) => x === document.activeElement);
      const n = items[(i + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length];
      if (n) { e.preventDefault(); n.focus(); }
    }
  });
}

async function wsPopover() {
  closeWsPop();
  const open = !!H.sid;                          // roots lock at session creation (header)
  const editable = !open;                        // only the no-session state arms the picker
  const shown = open ? (H.roots || []) : H.pendingRoots;
  const pop = document.createElement("div");
  pop.id = "h-ws-pop";
  pop.className = "st-card harness-popover harness-ws-pop";
  pop.setAttribute("role", "dialog");
  pop.setAttribute("aria-label", "Trusted folders");
  pop.innerHTML = `<div class="harness-popover__actions"><span class="card-title">Trusted folders</span>` +
    `<button class="st-btn st-btn--icon" id="h-ws-close" title="Close" aria-label="Close">✕</button></div>` +
    `<ul class="harness-ws-rows" id="h-ws-rows"></ul>` +
    (editable ? `<div class="harness-ws-add"><input class="st-input" id="h-ws-input" autocomplete="off"
        placeholder="Add folder: /data/workspaces/…" aria-label="Add a trusted folder">
      <div class="harness-ws-suggest" id="h-ws-suggest" role="listbox" hidden></div></div>` : "") +
    (editable ? `<p class="muted small">These folders apply to the next session you start.</p>`
      // the escape hatch must be a real control, not prose (UX review of the
      // M9 screenshot): name the buttons that actually change the fence
      : `<p class="muted small">This session keeps its roots for its whole lifetime.</p>
        <div class="harness-popover__actions">
          <button class="st-btn st-btn--secondary" id="h-ws-fork" title="Fork, narrowing the folders if you like">Fork with different folders…</button>
          <button class="st-btn st-btn--secondary" id="h-ws-new" title="Start a new session with these same folders">New session with these folders</button>
        </div>`);
  mountPopover(pop);
  $("h-ws").setAttribute("aria-expanded", "true");
  $("h-ws-close").onclick = () => { closeWsPop(); $("h-ws").focus(); };
  pop.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeWsPop(); $("h-ws").focus(); } });

  const rows = $("h-ws-rows");
  const render = () => {
    rows.innerHTML = "";
    const list = shown;
    if (!list.length) {
      rows.innerHTML = `<li class="muted small">server default workspace</li>`;
      return;
    }
    for (const r of list) {
      const li = document.createElement("li");
      li.innerHTML = `<code class="inline" title="${esc(r)}">${esc(r)}</code>` +
        (editable ? ` <button class="st-btn st-btn--icon" title="Remove" aria-label="Remove ${esc(r)}">✕</button>` : "");
      if (editable) li.querySelector("button").onclick = () => {
        H.pendingRoots = H.pendingRoots.filter((x) => x !== r);
        setWsChip(); render();
      };
      rows.appendChild(li);
    }
  };
  render();
  if (!editable) {
    $("h-ws-fork").onclick = () => { closeWsPop(); forkPopover(); };
    $("h-ws-new").onclick = () => {
      H.pendingRoots = [...(H.roots || [])];
      closeWsPop();
      startSession();
    };
    return;
  }

  // autocomplete against harness/ls: type a path, the picker browses it
  const input = $("h-ws-input"), suggest = $("h-ws-suggest");
  wireRootsBrowse(input, suggest);
  input.onkeydown = ((prev) => (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      const v = input.value.trim().replace(/\/+$/, "");
      if (!v) return;
      if (!H.pendingRoots.includes(v)) H.pendingRoots.push(v);
      input.value = ""; suggest.hidden = true;
      setWsChip(); render();
    } else prev(e);
  })(input.onkeydown);
}
$("h-ws").onclick = wsPopover;

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
$("h-preset").onchange = () => { store.set("harness.preset", $("h-preset").value); settingsSummary(); };

// ------------------------------------------------------------------ preset manage panel (M7 D9 / ADR-0011)
let PRESET_TOOLS = [];
let SKILL_NAMES = [];      // ADR-0018: the store's names, the skills input's placeholder hint
async function presetManage() {
  closePresetManage();
  const pop = document.createElement("div");
  pop.id = "h-preset-pop";
  pop.className = "st-card harness-popover harness-preset-pop";
  pop.setAttribute("role", "dialog");
  pop.setAttribute("aria-label", "Manage presets");
  pop.innerHTML = `<div class="harness-popover__actions">` +
    `<span class="card-title">Presets</span>` +
    `<button class="st-btn st-btn--primary" id="h-preset-new">New</button>` +
    `<button class="st-btn st-btn--icon" id="h-preset-close" title="Close" aria-label="Close">✕</button></div>` +
    `<div id="h-preset-rows"><p class="muted small">loading…</p></div>` +
    `<p class="muted small">Presets apply to new sessions; fork to change a running one. ` +
    `Bundled presets stay read-only — editing copies them to your presets.</p>`;
  mountPopover(pop);
  $("h-preset-close").onclick = () => { closePresetManage(); $("h-preset-manage").focus(); };
  $("h-preset-new").onclick = () => presetEditor(null);
  pop.addEventListener("keydown", (e) => { if (e.key === "Escape") { closePresetManage(); $("h-preset-manage").focus(); } });
  try {
    const [{presets}, {tools}, skills] = await Promise.all([
      hget("harness/presets"), hget("harness/tools"),
      hget("harness/skills").catch(() => ({skills: []}))]);   // ADR-0018: hint only, never blocks the panel
    PRESET_TOOLS = tools;
    SKILL_NAMES = skills.skills.map((s) => s.name);
    const rows = $("h-preset-rows");
    rows.innerHTML = "";
    for (const p of presets) {
      if (p.name === "default") continue;
      const row = document.createElement("div");
      row.className = "harness-preset-row";
      row.innerHTML = `<span class="harness-preset-row__name">${esc(p.name)}</span>` +
        `<span class="st-badge ${p.source === "user" ? "st-badge--reading" : ""}" title="${esc(p.source)} preset">${esc(p.source)}</span>`;
      const acts = document.createElement("span");
      acts.className = "harness-preset-row__actions";
      const mk = (label, title, fn) => {
        const b = document.createElement("button");
        b.className = "st-btn st-btn--secondary";
        b.textContent = label;
        b.title = title;
        b.onclick = fn;
        acts.appendChild(b);
        return b;
      };
      if (p.source === "user") {
        mk("Edit", "edit this preset", async () => {
          const one = await hget(`harness/preset?name=${encodeURIComponent(p.name)}`);
          presetEditor(one);
        });
        mk("Delete", "delete this preset", async (e) => {
          if (e.target.dataset.confirm !== "1") {           // second click confirms
            e.target.dataset.confirm = "1";
            e.target.textContent = "Sure?";
            return;
          }
          try { await hpost("harness/presets/delete", {name: p.name}); await loadPresets(); presetManage(); }
          catch (err) { toast("error", "Delete preset", err.message); }
        });
      } else {
        mk("Duplicate", "copy to your presets and edit", async () => {
          const one = await hget(`harness/preset?name=${encodeURIComponent(p.name)}`);
          presetEditor({...one, name: `${one.name}-copy`, dupOf: one.name});
        });
      }
      row.appendChild(acts);
      rows.appendChild(row);
    }
    if (!rows.children.length) rows.innerHTML = `<p class="muted small">No presets yet.</p>`;
  } catch (e) { toast("error", "Presets", e.message); }
}
function closePresetManage() {
  const pop = document.getElementById("h-preset-pop");
  if (pop) { unmountPopover(pop); pop.remove(); }
}
$("h-preset-manage").onclick = presetManage;

// ------------------------------------------------------------------ plugins panel (M8 / ADR-0014)
// Server-scoped: the button shows only when the server started with plugins
// on (GET harness/plugins answers; 404 = off). The panel reviews code — it
// never authors it (the filesystem is the editor, presets precedent).
let PLUGINS_ON = null;   // tri-state: unknown / off / on
async function pluginsOn() {
  if (PLUGINS_ON !== null) return PLUGINS_ON;
  try { await hget("harness/plugins"); PLUGINS_ON = true; }
  catch { PLUGINS_ON = false; }
  return PLUGINS_ON;
}
async function pluginBadge() {
  // warning dot when anything is quarantined/changed/failed (D1)
  if (!(await pluginsOn())) return;
  try {
    const {plugins} = await hget("harness/plugins");
    const need = plugins.filter((p) => p.state !== "active").length;
    const dot = $("h-plugin-dot");
    dot.hidden = need === 0;
    $("h-plugin-manage").setAttribute("aria-label",
      need ? `Manage plugins — ${need} plugin${need > 1 ? "s" : ""} need${need > 1 ? "" : "s"} review` : "Manage plugins");
  } catch { /* panel state is best-effort */ }
}
function closePluginManage() {
  const pop = document.getElementById("h-plugin-pop");
  if (pop) { unmountPopover(pop); pop.remove(); }
}
async function pluginManage() {
  closePresetManage();
  closePluginManage();
  const pop = document.createElement("div");
  pop.id = "h-plugin-pop";
  pop.className = "st-card harness-popover harness-preset-pop";
  pop.setAttribute("role", "dialog");
  pop.setAttribute("aria-label", "Manage plugins");
  pop.innerHTML = `<div class="harness-popover__actions">` +
    `<span class="card-title">Plugins</span>` +
    `<button class="st-btn st-btn--icon" id="h-plugin-close" title="Close" aria-label="Close">✕</button></div>` +
    `<div id="h-plugin-rows"><p class="muted small">loading…</p></div>` +
    `<p class="muted small">Plugins are code. Review the source before approving — ` +
    `a plugin loads only after you approve its exact bytes, and writes to the plugin ` +
    `folder always ask, even in yolo.</p>`;
  mountPopover(pop);
  $("h-plugin-close").onclick = () => { closePluginManage(); $("h-plugin-manage").focus(); };
  pop.addEventListener("keydown", (e) => { if (e.key === "Escape") { closePluginManage(); $("h-plugin-manage").focus(); } });
  try {
    const {dir, plugins} = await hget("harness/plugins");
    const rows = $("h-plugin-rows");
    rows.innerHTML = "";
    if (!plugins.length) {
      rows.innerHTML = `<p class="muted small">No plugins. Drop .py files into <code class="inline">${esc(dir)}</code> — they load only after you review them here.</p>`;
      return;
    }
    for (const p of plugins) {
      const row = document.createElement("div");
      row.className = "harness-preset-row";
      const badge = p.state === "active"
        ? `<span class="st-badge st-badge--reading" title="reviewed and loaded in new sessions">active</span>`
        : p.state === "failed"
          ? `<span class="st-badge st-badge--error" title="${esc(p.error || "import failed")}">error</span>`
          : `<span class="st-badge st-badge--queued" title="${p.state === "changed" ? "was approved, then edited — NOT loaded" : "never approved — NOT loaded"}">${p.state === "changed" ? "changed" : "needs review"}</span>`;
      row.innerHTML = `<span class="harness-preset-row__name">${esc(p.name)}</span>${badge}`;
      const acts = document.createElement("span");
      acts.className = "harness-preset-row__actions";
      const mk = (label, title, fn) => {
        const b = document.createElement("button");
        b.className = "st-btn st-btn--secondary";
        b.textContent = label;
        b.title = title;
        b.onclick = fn;
        acts.appendChild(b);
        return b;
      };
      mk("Source", "read the full source (and the diff vs the reviewed version)", () => pluginSource(p.name));
      if (p.state !== "active") {
        mk("Approve", "imports this code into every NEW session on this server — read the source first", async (e) => {
          if (e.target.dataset.confirm !== "1") {          // second click confirms
            e.target.dataset.confirm = "1";
            e.target.textContent = "Read source first?";
            return;
          }
          try {
            await hpost("harness/plugins/review", {name: p.name, sha256: p.sha256});
            toast("ok", "Plugin approved", `${p.name} loads in new sessions`);
            await pluginBadge(); presetManageRefresh(); pluginManage();
          } catch (err) { toast("error", "Approve plugin", err.message); }
        });
      } else {
        mk("Disable", "drop from the reviewed manifest — quarantines at the next session", async (e) => {
          if (e.target.dataset.confirm !== "1") {
            e.target.dataset.confirm = "1";
            e.target.textContent = "Sure?";
            return;
          }
          try { await hpost("harness/plugins/disable", {name: p.name}); await pluginBadge(); pluginManage(); }
          catch (err) { toast("error", "Disable plugin", err.message); }
        });
      }
      row.appendChild(acts);
      rows.appendChild(row);
      if (p.state === "failed" && p.error) {
        const err = document.createElement("p");
        err.className = "muted small harness-plugin-error";
        err.textContent = p.error;
        rows.appendChild(err);
      }
    }
  } catch (e) { toast("error", "Plugins", e.message); }
}
$("h-plugin-manage").onclick = pluginManage;
async function pluginSource(name) {
  closePluginManage();
  const pop = document.createElement("div");
  pop.id = "h-plugin-pop";
  pop.className = "st-card harness-popover harness-preset-pop harness-plugin-source";
  pop.setAttribute("role", "dialog");
  pop.setAttribute("aria-label", `Plugin source: ${name}`);
  pop.innerHTML = `<div class="harness-popover__actions">` +
    `<span class="card-title">Plugin <code class="inline">${esc(name)}</code></span>` +
    `<button class="st-btn st-btn--icon" id="h-plugin-close" title="Close" aria-label="Close">✕</button></div>` +
    `<div id="h-plugin-src"><p class="muted small">loading…</p></div>`;
  mountPopover(pop);
  $("h-plugin-close").onclick = () => { closePluginManage(); $("h-plugin-manage").focus(); };
  pop.addEventListener("keydown", (e) => { if (e.key === "Escape") { closePluginManage(); $("h-plugin-manage").focus(); } });
  try {
    const v = await hget(`harness/plugins/source?name=${encodeURIComponent(name)}`);
    const meta = `<p class="muted small">sha256 ${esc(v.sha256.slice(0, 16))}… · ${fmt(v.size)} bytes</p>`;
    let diffHtml = "";
    if (v.diff) {
      diffHtml = `<div class="tool-call__label">diff vs the reviewed version</div>` +
        `<pre class="st-code harness-plugin-diff">${v.diff.map((ln) =>
          `<span class="${ln.startsWith("+") && !ln.startsWith("+++") ? "harness-diff-add" : ln.startsWith("-") && !ln.startsWith("---") ? "harness-diff-del" : ""}">${esc(ln)}</span>`).join("\n")}</pre>`;
    }
    $("h-plugin-src").innerHTML = meta + diffHtml +
      `<div class="tool-call__label">full source</div>` +
      `<pre class="st-code harness-approval__full">${esc(v.source)}</pre>`;
  } catch (e) { $("h-plugin-src").innerHTML = `<p class="muted small">${esc(e.message)}</p>`; }
}
function presetManageRefresh() { /* the preset editor's tool list may gain plugin tools */ }

// the editor form over the YAML schema: the file stays the source of truth
function presetEditor(preset) {
  const editing = preset && !preset.dupOf;
  const pop = document.createElement("div");
  pop.className = "st-card harness-popover harness-preset-pop";
  pop.setAttribute("role", "dialog");
  pop.setAttribute("aria-label", editing ? "Edit preset" : "New preset");
  const allow = new Set((preset && preset.tools && preset.tools.allow) || []);
  const deny = new Set((preset && preset.tools && preset.tools.deny) || []);
  pop.innerHTML = `<span class="card-title">${editing ? `Edit ${esc(preset.name)}` : (preset ? "Duplicate preset" : "New preset")}</span>` +
    `<label class="muted small" for="h-pe-name">name</label>` +
    `<input class="st-input" id="h-pe-name" value="${esc((preset && preset.name) || "")}" ${editing ? "disabled" : ""}>` +
    `<label class="muted small" for="h-pe-desc">description</label>` +
    `<input class="st-input" id="h-pe-desc" value="${esc((preset && preset.description) || "")}">` +
    `<label class="muted small" for="h-pe-persona">persona (system prompt)</label>` +
    `<textarea class="st-input" id="h-pe-persona" rows="4">${esc((preset && preset.persona) || "")}</textarea>` +
    `<label class="muted small" for="h-pe-skills">skills (comma-separated names; empty = all visible)</label>` +
    `<input class="st-input" id="h-pe-skills" value="${esc(((preset && preset.skills) || []).join(", "))}"` +
    ` placeholder="${esc(SKILL_NAMES.join(", ") || "leave empty for all")}">` +
    `<span class="muted small">tools: allow = only these run; deny = these never run</span>` +
    `<div class="harness-preset-tools" id="h-pe-tools"></div>` +
    `<div class="harness-popover__actions">` +
    `<button class="st-btn st-btn--primary" id="h-pe-save">Save</button>` +
    `<button class="st-btn st-btn--secondary" id="h-pe-cancel">Cancel</button></div>`;
  mountPopover(pop);
  const box = $("h-pe-tools");
  for (const t of PRESET_TOOLS) {
    const lab = document.createElement("label");
    lab.className = "harness-preset-tool";
    lab.innerHTML = `<span>${esc(t)}</span>` +
      `<label><input type="checkbox" data-t="${esc(t)}" data-k="allow" ${allow.has(t) ? "checked" : ""}>allow</label>` +
      `<label><input type="checkbox" data-t="${esc(t)}" data-k="deny" ${deny.has(t) ? "checked" : ""}>deny</label>`;
    box.appendChild(lab);
  }
  // allow and deny are mutually exclusive per tool
  box.addEventListener("change", (e) => {
    const cb = e.target;
    const other = box.querySelector(`input[data-t="${cb.dataset.t}"][data-k="${cb.dataset.k === "allow" ? "deny" : "allow"}"]`);
    if (cb.checked && other) other.checked = false;
  });
  $("h-pe-cancel").onclick = () => { unmountPopover(pop); pop.remove(); presetManage(); };
  $("h-pe-save").onclick = async () => {
    const name = $("h-pe-name").value.trim();
    const a = new Set(), dn = new Set();
    for (const cb of box.querySelectorAll("input:checked")) (cb.dataset.k === "allow" ? a : dn).add(cb.dataset.t);
    try {
      await hpost("harness/presets", {name, description: $("h-pe-desc").value.trim() || null,
                                      persona: $("h-pe-persona").value.trim() || null,
                                      skills: (() => {
                                        const s = $("h-pe-skills").value.split(",").map((x) => x.trim()).filter(Boolean);
                                        return s.length ? s : null;   // ADR-0018: null = all visible
                                      })(),
                                      tools: {allow: [...a], deny: [...dn]}});
      unmountPopover(pop);
      pop.remove();
      await loadPresets();
      presetManage();
      toast("success", "Preset saved", name);
    } catch (e) { toast("error", "Save preset", e.message); }
  };
  $("h-pe-name").focus();
}

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
  mountPopover(pop);
  $("h-export-md").focus();
  $("h-export-md").onclick = () => downloadExport("markdown");
  $("h-export-jsonl").onclick = () => downloadExport("jsonl");
  pop.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeExportPopover(); $("h-export").focus(); } });
}
function closeExportPopover() {
  const pop = document.getElementById("h-export-pop");
  if (pop) { unmountPopover(pop); pop.remove(); }
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

// ------------------------------------------------------- stats drawer (M11a, ADR-0017)
// Read + render only: the numbers come from GET /harness/session/stats (the
// server-side fold, ADR-0017) — never re-derived here. The drawer renders on
// open and re-renders when a turn/end lands while it is open (paint() hooks
// it): one pump, two consumers, no second poll loop.
const STATS_CAP = 50;      // turns rendered before "show all" (the sparkline always covers all)

function statsPopover() {
  closeStatsPopover();
  const pop = document.createElement("div");
  pop.id = "h-stats-pop";
  pop.className = "st-card harness-popover harness-stats";
  pop.setAttribute("role", "dialog");
  pop.setAttribute("aria-label", "Session stats");
  pop.innerHTML = `<p class="muted small">Loading stats&hellip;</p>`;
  mountPopover(pop);
  $("h-stats").setAttribute("aria-expanded", "true");
  H.statsAll = false;
  pop.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeStatsPopover(); $("h-stats").focus(); } });
  statsRender();
}
function closeStatsPopover() {
  const pop = document.getElementById("h-stats-pop");
  if (pop) { unmountPopover(pop); pop.remove(); }
  $("h-stats").setAttribute("aria-expanded", "false");
}
function statsTick() {                       // paint()'s turn/end hook: refresh only if open
  if (document.getElementById("h-stats-pop")) statsRender();
}
async function statsRender() {
  const pop = document.getElementById("h-stats-pop");
  if (!pop || !H.sid) return;
  const sid = H.sid;
  let st;
  try { st = await hget(`harness/session/stats?session=${encodeURIComponent(sid)}`); }
  catch (e) { if (pop.isConnected) pop.innerHTML = `<p class="muted small">stats failed: ${esc(e.message)}</p>`; return; }
  if (!pop.isConnected || H.sid !== sid) return;   // session switched / popover closed mid-fetch
  pop.innerHTML = statsNowHtml(st) + statsTurnsHtml(st) + statsTotalsHtml(st);
  const more = document.getElementById("h-stats-more");
  if (more) more.onclick = () => { H.statsAll = !H.statsAll; statsRender(); };
}
function statsNowHtml(st) {
  // silence at defaults (ADR-0016 §2): the row only earns its place when the
  // session is live or something is pending — the transcript says idle.
  const t = st.turns[st.turns.length - 1];
  const open = t && t.open;
  const pendingTools = open ? t.tools.pending : 0;
  if (!H.busy && !pendingTools && !st.children.length) return "";
  const bits = [];
  if (H.busy) bits.push(`<span class="st-badge st-badge--generating">working</span>`);
  if (pendingTools) bits.push(`<span class="muted small">${pendingTools} tool${pendingTools > 1 ? "s" : ""} in flight</span>`);
  if (st.totals.lastStepMs != null) bits.push(`<span class="muted small">last step ${fmt(st.totals.lastStepMs)} ms</span>`);
  if (st.children.length) bits.push(`<span class="muted small">${st.children.length} subagent${st.children.length > 1 ? "s" : ""}</span>`);
  return `<div class="harness-stats__now">${bits.join("")}</div>`;
}
const STATS_REASON = {completed: "completed", aborted: "cancelled", error: "error",
                      blocked: "blocked", "max-tokens": "token limit", interrupted: "interrupted", forked: "forked"};
function statsTurnsHtml(st) {
  if (!st.turns.length) return `<span class="card-title">Turns</span><p class="muted small">No turns yet.</p>`;
  const all = st.turns;
  const shown = (H.statsAll || all.length <= STATS_CAP) ? all : all.slice(-STATS_CAP);
  const rows = shown.map((t) => {
    const tools = t.tools.ok + t.tools.error + t.tools.unknown + t.tools.pending;
    const bits = [`${t.steps} step${t.steps === 1 ? "" : "s"}`];
    if (tools) {
      const tb = [t.tools.ok && `${t.tools.ok} ok`, t.tools.error && `${t.tools.error} error`,
                  t.tools.unknown && `${t.tools.unknown} unknown`, t.tools.pending && `${t.tools.pending} running`].filter(Boolean).join(" · ");
      bits.push(`${tools} tool${tools === 1 ? "" : "s"} (${tb})`);
    }
    const ap = t.approvals;
    if (ap.asked) bits.push(`${ap.asked} asked · ${ap.allowed} allowed · ${ap.denied} denied${ap.cancelled ? ` · ${ap.cancelled} cancelled` : ""}`);
    if (ap.auto) bits.push(`${ap.auto} auto`);
    if (t.ms != null) bits.push(`${fmt(t.ms / 1000, 1)}s`);
    const badge = t.open ? `<span class="st-badge st-badge--queued">running</span>`
      : `<span class="st-badge st-badge--${t.reason === "completed" ? "reading" : "error"}">${esc(STATS_REASON[t.reason] || t.reason || "?")}</span>`;
    const ctx = t.ctxPct == null ? "" : `<span class="harness-stats__ctx" title="context at end of turn (same number as the pill)">${fmt(t.ctxPct, 1)}%</span>`;
    const comp = t.compaction ? `<span class="muted small"> · compaction</span>` : "";
    const expand = t.toolNames.length
      ? `<details class="harness-stats__tools"><summary class="muted small">${t.toolNames.length} tool call${t.toolNames.length === 1 ? "" : "s"}</summary>` +
        `<ul>${t.toolNames.map((e) => `<li><span>${esc(e.name)}</span>` +
          `<span class="st-badge st-badge--${e.outcome === "ok" ? "reading" : e.outcome === "pending" ? "queued" : "error"}">${esc(e.outcome)}</span>` +
          (e.spill ? `<span class="muted small">spilled</span>` : "") + `</li>`).join("")}</ul></details>`
      : "";
    return `<li class="harness-stats__turn"><div class="harness-stats__turnrow">` +
      `<span class="harness-stats__n">#${t.n}</span>${badge}<span class="muted small">${bits.join(" · ")}${comp}</span>${ctx}</div>${expand}</li>`;
  }).join("");
  const more = all.length > STATS_CAP
    ? `<button class="st-btn st-btn--secondary" id="h-stats-more">${H.statsAll ? "Show fewer" : `Show all ${all.length} turns`}</button>` : "";
  return `<span class="card-title">Turns</span><ol class="harness-stats__turns">${rows}</ol>` +
    statsSparkline(all) + more;
}
function statsSparkline(turns) {
  // context curve: one polyline of ctx% per turn, compaction turns get a tick.
  // Inline SVG only (ADR-0005: no chart library). Cheap enough to cover all turns.
  const pts = turns.map((t) => t.ctxPct).filter((v) => v != null);
  if (pts.length < 2) return "";
  const w = 520, h = 48, max = Math.max(100, ...pts);
  const x = (i) => (i / (pts.length - 1)) * (w - 8) + 4;
  const y = (v) => h - 4 - (v / max) * (h - 8);
  const line = pts.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const ticks = turns.map((t, i) => t.compaction ? `<line x1="${x(i).toFixed(1)}" y1="2" x2="${x(i).toFixed(1)}" y2="${h - 2}" class="harness-stats__tick"/>` : "").join("");
  const label = `context ${Math.min(...pts).toFixed(0)}% to ${Math.max(...pts).toFixed(0)}%, now ${pts[pts.length - 1].toFixed(0)}%`;
  return `<svg class="harness-stats__spark" viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(label)}">` +
    `<polyline points="${line}"/>${ticks}</svg>`;
}
function statsTotalsHtml(st) {
  const T = st.totals;
  const ap = T.approvals;
  const bits = [`${T.turns} turn${T.turns === 1 ? "" : "s"}`, `${T.steps} step${T.steps === 1 ? "" : "s"}`];
  const tools = T.tools.ok + T.tools.error + T.tools.unknown + T.tools.pending;
  if (tools) bits.push(`${tools} tool${tools === 1 ? "" : "s"} (${T.tools.ok} ok${T.tools.error ? `, ${T.tools.error} error` : ""}${T.tools.unknown ? `, ${T.tools.unknown} unknown` : ""}${T.tools.pending ? `, ${T.tools.pending} running` : ""})`);
  if (ap.asked) bits.push(`${ap.allowed} allowed · ${ap.denied} denied${ap.cancelled ? ` · ${ap.cancelled} cancelled` : ""}`);
  if (ap.auto) bits.push(`${ap.auto} auto`);
  if (T.tokens.prompt || T.tokens.completion) bits.push(`${kfmt(T.tokens.prompt)} in / ${kfmt(T.tokens.completion)} out`);
  if (T.ms) bits.push(`${fmt(T.ms / 1000, 1)}s total`);
  return `<span class="card-title">Totals</span><p class="harness-stats__totals muted small">${bits.join(" · ")}</p>`;
}
$("h-stats").onclick = () => { if (H.sid) statsPopover(); };

// ------------------------------------------------------------------ the stream
function openSession(sid) {
  H.sid = sid;
  const row = H.sessions.find((s) => s.id === sid);
  H.roots = (row && row.roots) || [];               // M9: the chip shows this session's roots
  if (H.roots.length) H.pendingRoots = [...H.roots];  // the picker defaults to the last session's
  setWsChip();
  store.set("harness.session", sid);
  composerEnabled(true);   // boot may have disabled it (no sessions yet): opening one must re-enable
  H.lastSeq = -1;
  H.seenAsks = new Set();
  H.askMeta = {};
  H.buffer = [];
  H.retryDelay = 1000;
  H.plan = false; H.goal = null;
  H.ctxEstimate = 0;                       // the pill re-arms from the meter fold on subscribe
  closeStatsPopover();                     // M11a: never show the previous session's stats
  setPlanChip(false); setGoalChip(null, "");   // no stale chips between sessions
  setBusy(false);
  $("h-policy").value = "ask"; $("h-allowlist").hidden = true; setYoloBanner(false);
  settingsSummary();
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
      provClear();
      H.lastSeq = -1;
      H.seenAsks = new Set();
      H.ctxEstimate = 0;                   // the subscribe-time meter fold re-arms the pill
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
    const {events, pending, busy, policy, context} = await hget(`harness/events?session=${encodeURIComponent(H.sid)}`);
    const buffered = H.buffer; H.buffer = [];
    $("h-transcript").innerHTML = "";
    provClear();
    H.lastSeq = -1;
    H.seenAsks = new Set();
    if (context) H.ctxEstimate = context.estimate;   // the meter fold: the pill's source of truth
    for (const row of events) paint(row);
    for (const row of pending) paint(row);            // unanswered asks: redraw their cards
    for (const row of buffered) paint(row);
    H.busy = busy; setBusy(busy);
    if (policy) {                                      // the server's live policy wins (ADR-0010)
      $("h-policy").value = policy.mode;
      $("h-allowlist").hidden = policy.mode !== "allowlist";
      $("h-allowlist").value = (policy.allowlist || []).join(", ");
      setYoloBanner(policy.mode === "yolo");
    }
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

// provisional streaming nodes (M7a-3): process-local, replaced by the durable row
const PROV = { think: null, thinkBody: null, bubble: null };
function provThink() {
  if (!PROV.think) {
    PROV.think = el("st-msg st-msg--assistant harness-prov");
    const det = document.createElement("details");
    det.className = "st-collapse think";
    det.open = true;
    det.innerHTML = `<summary>${icon("thinking", "st-icon st-icon--sm")}<span class="think-title">Thinking…</span>` +
      `${icon("chevron", "st-icon st-icon--sm st-chev")}</summary>` +
      `<div class="st-collapse__body thinking"></div>`;
    PROV.think.appendChild(det);
    PROV.thinkBody = det.querySelector(".thinking");
  }
  return PROV.thinkBody;
}
function provBubble() {
  if (!PROV.bubble) {
    PROV.bubble = el("st-msg st-msg--assistant harness-prov");
    PROV.bubble.setAttribute("aria-hidden", "true");   // the durable row announces it (a11y)
    const b = document.createElement("div");
    b.className = "st-bubble";
    b.style.whiteSpace = "pre-wrap";
    PROV.bubble.appendChild(b);
  }
  return PROV.bubble.querySelector(".st-bubble");
}
function provClear() {
  for (const k of ["think", "bubble"]) if (PROV[k]) { PROV[k].remove(); PROV[k] = null; }
  PROV.thinkBody = null;
}

// the durable thinking block (M7a-1): collapsed once settled — the "why" is
// available, not mandatory reading (same shape as the Chat tab's .think)
function thinkBlock(reasoning) {
  const wrap = el("st-msg st-msg--assistant");
  const det = document.createElement("details");
  det.className = "st-collapse think";
  det.innerHTML = `<summary>${icon("thinking", "st-icon st-icon--sm")}<span class="think-title">Thinking</span>` +
    `${icon("chevron", "st-icon st-icon--sm st-chev")}</summary>` +
    `<div class="st-collapse__body thinking"></div>`;
  det.querySelector(".thinking").textContent = reasoning;
  wrap.appendChild(det);
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
  if (t === "stream/delta") {
    // provisional (M7a-3): the durable assistant/message finalizes it
    if (d.kind === "reasoning") provThink().textContent += d.text || "";
    else if (d.kind === "content") {
      if (PROV.think) { const det = PROV.think.querySelector("details"); if (det) det.open = false; }
      provBubble().textContent += d.text || "";
    }
    else if (d.kind === "context") { H.ctxEstimate = d.estimate; renderCtxPill(d.estimate); }  // the meter's fold: the pill's source of truth
    const stick = nearBottom();
    if (stick) scrollDown(true);
    return;
  }
  if (t === "user/message") {
    provClear();
    if (d.source === "subagent") {
      subagentCard(d);
    } else {
      el("st-msg st-msg--user", `<div class="st-bubble">${esc(d.content)}</div>`);
    }
    H.busy = true; setBusy(true);
  } else if (t === "assistant/message") {
    provClear();
    if (d.reasoning) thinkBlock(d.reasoning);
    if (d.content) el("st-msg st-msg--assistant", `<div class="st-bubble">${markdown(d.content)}</div>`);
    if (d.usage) showUsage(d.usage);
  } else if (t === "session/title") {
    loadSessions();
  } else if (t === "tool/call") {
    H.askMeta = H.askMeta || {};
    H.askMeta[d.call_id] = {tool: d.name, args: d.arguments};   // full payload for the approval fold (replay)
    toolCard(d);
  } else if (t === "tool/result") {
    fillToolCard(d);
  } else if (t === "approval/ask") {
    if (H.seenAsks.has(d.call_id)) return;
    H.seenAsks.add(d.call_id);
    approvalCard(d);
  } else if (t === "approval/asked") {
    // the durable twin of the synthetic ask: on a live stream the card is
    // already painted (skip); on replay it is the only record — remember the
    // payload so the decided row can fold it (M7 review: history must show
    // what was approved, not just the live moment)
    H.askMeta = H.askMeta || {};
    H.askMeta[d.call_id] = {...(H.askMeta[d.call_id] || {}), tool: d.tool, preview: d.preview};
  } else if (t === "approval/decided") {
    if (!markDecided(d.call_id, d.outcome)) {
      const meta = (H.askMeta || {})[d.call_id] || {};
      const full = meta.args ? (meta.args.command || JSON.stringify(meta.args, null, 1)) : (meta.preview || "");
      approvalFold(meta.tool || d.tool || "tool", d.outcome, full);
    }
  } else if (t === "question/ask") {
    if (H.seenAsks.has(d.qid)) return;
    H.seenAsks.add(d.qid);
    questionCard(d);
  } else if (t === "turn/end") {
    provClear();                                       // a cancelled/errored turn clears provisional nodes
    H.busy = false; setBusy(false);
    statsTick();                                       // M11a: the open stats drawer re-renders (one pump, two consumers)
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
  // the fold rule (user, 2026-10-09): a settled tool call folds to its quiet
  // one-line row — history must not compete with the live step (same rule as
  // decided approvals). It stays open only on error: the failure IS the live
  // thing to read. (Was: open forever, only re-opened on error — the card
  // never folded when done.)
  details.open = !!d.is_error;
}

function setBusy(busy) {
  H.busy = busy;
  const pill = $("h-state");
  // while yolo is on, the pill carries the warn colour (D6): working + unattended
  const yolo = !$("h-yolo").hidden;
  pill.dataset.state = busy ? (yolo ? "yolo" : "generating") : "idle";
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

function renderCtxPill(used) {
  const max = (lastMetrics && lastMetrics.engine && lastMetrics.engine.max_context) || 0;
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

function showUsage(u) {
  // single source of truth (2026-10-09 ctx-pill 0% bug): the meter's fold —
  // the same number that drives compaction — wins over the last request's
  // raw prompt_tokens (which only covers one turn and was 0 on the in-process
  // path). The request count is the fallback when no fold has landed yet.
  renderCtxPill(H.ctxEstimate || (u && u.prompt_tokens) || 0);
}

// ------------------------------------------------------------------ approvals + questions
// the ask card shows the FULL payload (M7a-2 / ADR-0010): never approve what
// you cannot read. shell: the whole command, scrollable. write/edit: the path
// is the hero line, the content sits behind an expand.
// M9: the human sees which sandbox a call lives in — the trusted root that
// contains the path (or the shell's cwd). No match = outside the fence (the
// tool will refuse it, but the card says so before the decision).
function wsContextLine(path) {
  const roots = H.roots || [];
  if (!roots.length || !path) return "";
  const hit = roots.find((r) => path === r || path.startsWith(r.replace(/\/+$/, "") + "/"));
  return `<p class="muted small harness-ws-line">${icon("attach", "st-icon st-icon--sm")} ` +
    (hit ? `inside <code class="inline">${esc(hit)}</code>` : `outside every trusted folder — will be refused`) +
    `</p>`;
}

function approvalHero(d) {
  const a = d.args || {};
  if (d.tool === "shell") {
    return `<div class="harness-approval__head">${icon("warning", "st-icon st-icon--sm")}` +
      `<span class="card-title">Run a shell command?${d.session ? ` <span class="st-badge st-badge--generating" title="asked by subagent ${esc(d.session)}">subagent</span>` : ""}</span></div>` +
      `<pre class="st-code harness-approval__full">${esc(a.command || d.preview || "")}</pre>` +
      wsContextLine(a.cwd || "");
  }
  if (d.tool === "write_file") {
    const n = String(a.content || "").length;
    const pw = d.plugin_write
      ? ` <span class="st-badge st-badge--queued" title="writes into the plugin folder — this file becomes server code after review in the Plugins panel">plugin write</span>` : "";
    return `<div class="harness-approval__head">${icon("warning", "st-icon st-icon--sm")}` +
      `<span class="card-title">Write <code class="inline">${esc(a.path || "")}</code> — ${fmt(n)} chars${d.session ? ` <span class="st-badge st-badge--generating" title="asked by subagent ${esc(d.session)}">subagent</span>` : ""}${pw}</span></div>` +
      (d.plugin_write ? `<p class="muted small">This file becomes server code after you review it in the Plugins panel. It will NOT load automatically.</p>` : "") +
      `<details class="st-collapse"><summary>show content${icon("chevron", "st-icon st-icon--sm st-chev")}</summary>` +
      `<div class="st-collapse__body"><pre class="st-code harness-approval__full">${esc(a.content || "")}</pre></div></details>` +
      wsContextLine(a.path || "");
  }
  if (d.tool === "edit_file") {
    const pw = d.plugin_write
      ? ` <span class="st-badge st-badge--queued" title="edits inside the plugin folder — this file becomes server code after review in the Plugins panel">plugin write</span>` : "";
    return `<div class="harness-approval__head">${icon("warning", "st-icon st-icon--sm")}` +
      `<span class="card-title">Edit <code class="inline">${esc(a.path || "")}</code>${a.replace_all ? " (all occurrences)" : ""}${d.session ? ` <span class="st-badge st-badge--generating" title="asked by subagent ${esc(d.session)}">subagent</span>` : ""}${pw}</span></div>` +
      (d.plugin_write ? `<p class="muted small">This file becomes server code after you review it in the Plugins panel. It will NOT load automatically.</p>` : "") +
      `<details class="st-collapse" open><summary>find &rarr; replace${icon("chevron", "st-icon st-icon--sm st-chev")}</summary>` +
      `<div class="st-collapse__body"><div class="tool-call__label">find</div>` +
      `<pre class="st-code harness-approval__full">${esc(a.old_string || "")}</pre>` +
      `<div class="tool-call__label">replace</div>` +
      `<pre class="st-code harness-approval__full">${esc(a.new_string || "")}</pre></div></details>` +
      wsContextLine(a.path || "");
  }
  const mcp = d.tool.includes("__")   // namespaced = an MCP hub tool (ADR-0012): out of process
    ? `<span class="st-badge st-badge--reading" title="MCP server &quot;${esc(d.tool.split("__")[0])}&quot; — runs out of process">mcp</span>` : "";
  const pl = d.plugin   // a reviewed plugin's own tool (ADR-0014): in-process server code
    ? ` <span class="st-badge st-badge--queued" title="provided by a self-authored plugin — runs in-process as server code">plugin</span>` : "";
  return `<div class="harness-approval__head">${icon("warning", "st-icon st-icon--sm")}` +
    `<span class="card-title">Run <code class="inline">${esc(d.tool)}</code>? ${mcp}${pl}${d.session ? ` <span class="st-badge st-badge--generating" title="asked by subagent ${esc(d.session)}">subagent</span>` : ""}</span></div>` +
    `<pre class="st-code harness-approval__full">${esc(JSON.stringify(a, null, 1) || d.preview || "")}</pre>` +
    wsContextLine(a.path || a.file_path || "");
    }
function approvalCard(d) {
  const card = el("harness-approval",
    approvalHero(d) +
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
  if (!card) return false;
  // the decision is made: fold the card to one quiet line (M6 UX: a decided
  // approval must not compete with the live step; the FULL payload stays available)
  const full = [...card.querySelectorAll("pre")].map((p) => p.textContent).join("\n");
  const head = card.querySelector(".harness-approval__head");
  const title = head.querySelector(".card-title")?.textContent || "";
  approvalFold(title.trim(), outcome, full, card);
  return true;
}

// the quiet decided-approval line, shared by the live fold and the replay path
// (a yolo decision has no ask card: it lands straight as a fold)
function approvalFold(title, outcome, full, replaceEl) {
  const html =
    `<details class="harness-approval__fold">` +
    `<summary>${icon(outcome === "deny" ? "error" : "check", "st-icon st-icon--sm")}` +
    `<span class="tool-call__name">${esc(title)}</span>` +
    `<span class="st-badge ${outcome === "deny" ? "st-badge--error" : "st-badge--reading"}">${esc(outcome)}</span>` +
    `${icon("chevron", "st-icon st-icon--sm st-chev")}</summary>` +
    `<div class="st-collapse__body"><pre class="tool-call__pre">${esc(full)}</pre></div></details>`;
  if (replaceEl) {
    replaceEl.dataset.decided = outcome;
    replaceEl.innerHTML = html;
  } else {
    const node = el("harness-approval", html);
    node.dataset.decided = outcome;   // the quiet fold style, replay path too
  }
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
  for (const id of ["h-plan", "h-goal", "h-fork", "h-settings", "h-export"]) $(id).disabled = !on;
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
  mountPopover(pop);
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
  });
  // Escape binds on the popover (like every other dialog), not just the input:
  // scrim-click / global-Escape dispatch the synthetic key to the popover node
  pop.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeGoalPopover(); $("h-goal").focus(); } });
}
function closeGoalPopover() {
  const pop = document.getElementById("h-goal-pop");
  if (pop) { unmountPopover(pop); pop.remove(); }
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
  if (text.startsWith("/")) {                 // ADR-0019: commands never reach the model
    $("h-input").value = "";
    autosizeHarness();
    await runCommand(text);
    return;
  }
  if (!H.sid) { toast("info", "No session", "Start one with the + button."); return; }
  $("h-input").value = "";
  autosizeHarness();
  try {
    const body = {content: text, target: H.busy ? "steer" : "auto"};
    await hpost(`harness/session/${H.sid}/message`, body);
  } catch (err) { toast("error", "Send", err.message); }
};

// ------------------------------------------------------- slash commands (M11c, ADR-0019)
// Client-side dispatch over the routes the buttons already call. Command
// replies are local transcript notes (the log stays the session's truth,
// ADR-0004). Built-in names beat skill names; a leading "/" is never a task.
const COMMANDS = [
  {name: "new",     group: "Session", help: "start a new session",            needs: false, run: () => startSession()},
  {name: "fork",    group: "Session", help: "fork this session",              needs: true,  run: () => forkPopover()},
  {name: "title",   group: "Session", help: "rename this session",            needs: true,  args: true, run: async (a) => {
      if (!a) return "usage: /title <new name>";
      await hpost(`harness/session/${H.sid}/title`, {text: a});
      await loadSessions();
      return `session titled: ${a}`; }},
  {name: "roots",   group: "Session", help: "show this session's trusted folders", needs: true, run: () => wsPopover()},
  {name: "preset",  group: "Session", help: "open Settings (preset applies to new sessions)", needs: false, run: (a) => {
      settingsPopover();
      if (a) { const sel = $("h-preset"); if ([...sel.options].some((o) => o.value === a)) { sel.value = a; sel.onchange(); } }
      return null; }},
  {name: "compact", group: "Run", help: "compact the context now (idle only)", needs: true, run: async () => {
      try { const r = await hpost(`harness/session/${H.sid}/compact`, {}); return `compacted: ${r.detail}`; }
      catch (e) { return e.message; } }},   // the 409 envelope already names the command
  {name: "cancel",  group: "Run", help: "stop the current turn",              needs: true, run: async () => {
      await hpost(`harness/session/${H.sid}/cancel`, {}); return "cancel requested"; }},
  {name: "stop",    group: "Run", help: "= /cancel",                          needs: true, hidden: true, run: async () => {
      await hpost(`harness/session/${H.sid}/cancel`, {}); return "cancel requested"; }},
  {name: "export",  group: "Run", help: "download the session (md|jsonl)",    needs: true, args: true, run: (a) => {
      if (a === "md" || a === "jsonl") { downloadExport(a === "md" ? "markdown" : "jsonl"); return null; }
      if (a) return "usage: /export [md|jsonl]";
      exportPopover(); return null; }},
  {name: "status",  group: "Info", help: "session summary",                   needs: true, run: () => {
      const roots = (H.roots && H.roots.length) ? H.roots.join(", ") : "server default";
      return `session ${H.sid} · preset ${$("h-preset").value} · ${H.busy ? "working" : "idle"} · folders: ${roots}`; }},
  {name: "context", group: "Info", help: "context numbers (the pill's)",      needs: false, run: () => {
      const ctx = $("h-ctx").textContent || "no reading yet";
      return `context: ${ctx} (same meter fold as the pill and compaction)`; }},
  {name: "skills",  group: "Info", help: "list skills (each is also a command)", needs: false, run: async () => {
      const {skills, warnings} = await hget("harness/skills");
      if (!skills.length) return "no skills installed";
      return skills.map((s) => `/${s.name} — ${s.description}`).concat(warnings.map((w) => `(warning: ${w})`)).join("\n"); }},
  {name: "help",    group: "Info", help: "this list",                         needs: false, run: () => helpText()},
];

function helpText() {
  const groups = {};
  for (const c of COMMANDS) if (!c.hidden) (groups[c.group] = groups[c.group] || []).push(`/${c.name}${c.args ? " …" : ""} — ${c.help}`);
  const lines = Object.entries(groups).map(([g, items]) => `${g}: ${items.join(" · ")}`);
  lines.push(`+ ${SKILL_NAMES.length} skill command${SKILL_NAMES.length === 1 ? "" : "s"} (/skills to list)`);
  return lines.join("\n");
}

async function runCommand(text) {
  const m = text.match(/^\/([A-Za-z0-9_-]+)\s*([\s\S]*)$/);
  const name = m ? m[1].toLowerCase() : "";
  const args = (m ? m[2] : "").trim();
  let cmd = COMMANDS.find((c) => c.name === name);
  if (!cmd) {
    // skill invocation (Hermes pattern): the MODEL pulls the body via
    // skill_view — progressive disclosure stays intact (ADR-0018/0019).
    if (SKILL_NAMES.includes(name)) {
      if (!H.sid) { toast("info", "No session", "Start one with the + button."); return; }
      await hpost(`harness/session/${H.sid}/message`,
                  {content: `load skill '${name}' with skill_view and follow it${args ? ` for: ${args}` : ""}`,
                   target: H.busy ? "steer" : "auto"});
      return;
    }
    toast("warn", "Unknown command", `/${name} — /help lists the commands`);
    return;
  }
  if (cmd.needs && !H.sid) { toast("info", "No session", `/${cmd.name} needs an open session.`); return; }
  const reply = await cmd.run(args);
  if (reply) el("harness-note", `<span class="muted small">${esc(reply).replace(/\n/g, "<br>")}</span>`);
}

// autocomplete: "/" at the composer start filters commands + skills
let AC = {open: false, items: [], idx: -1};
function acClose() {
  AC.open = false; AC.items = []; AC.idx = -1;
  const p = document.getElementById("h-ac");
  if (p) p.remove();
  $("h-input").removeAttribute("aria-activedescendant");
}
function acUpdate() {
  const v = $("h-input").value;
  if (!v.startsWith("/") || v.slice(1).includes(" ") || v.slice(1).includes("\n")) { acClose(); return; }
  const q = v.slice(1).toLowerCase();
  const cmds = COMMANDS.filter((c) => !c.hidden && c.name.startsWith(q))
    .map((c) => ({name: c.name, help: c.help, kind: "cmd"}));
  const skills = SKILL_NAMES.filter((s) => s.toLowerCase().startsWith(q) && !COMMANDS.some((c) => c.name === s.toLowerCase()))
    .map((s) => ({name: s, help: "skill", kind: "skill"}));
  const items = [...cmds, ...skills];
  if (!items.length) { acClose(); return; }
  AC.items = items; AC.open = true;
  if (AC.idx >= items.length) AC.idx = -1;
  let pop = document.getElementById("h-ac");
  if (!pop) {
    pop = document.createElement("div");
    pop.id = "h-ac";
    pop.setAttribute("role", "listbox");
    pop.setAttribute("aria-label", "Command autocomplete");
    $("h-composer").appendChild(pop);
  }
  pop.innerHTML = items.map((it, i) =>
    `<div class="harness-ac__item${i === AC.idx ? " is-active" : ""}" role="option" id="h-ac-${i}"` +
    ` aria-selected="${i === AC.idx}"><strong>/${esc(it.name)}</strong><span class="muted small">${esc(it.help)}</span></div>`).join("");
}
function acAccept(explicit) {
  // Tab is explicit completion: a unique match completes even unselected.
  // Enter with nothing selected runs the typed text as a command.
  if (AC.idx < 0) {
    if (!explicit || AC.items.length !== 1) return false;
    AC.idx = 0;
  }
  $("h-input").value = "/" + AC.items[AC.idx].name + " ";
  acClose();
  return true;
}
$("h-input").addEventListener("input", acUpdate);
$("h-input").addEventListener("blur", () => setTimeout(acClose, 150));
$("h-input").addEventListener("keydown", (e) => {
  if (!AC.open) return;
  if (e.key === "ArrowDown") { e.preventDefault(); AC.idx = (AC.idx + 1) % AC.items.length; acRepaintIdx(); }
  else if (e.key === "ArrowUp") { e.preventDefault(); AC.idx = (AC.idx - 1 + AC.items.length) % AC.items.length; acRepaintIdx(); }
  else if (e.key === "Tab") { e.preventDefault(); acAccept(true); }
  else if (e.key === "Enter" && AC.idx >= 0) { e.preventDefault(); acAccept(false); }
  else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); acClose(); }
});
function acRepaintIdx() {
  const pop = document.getElementById("h-ac");
  if (!pop) return;
  [...pop.children].forEach((c, i) => { c.classList.toggle("is-active", i === AC.idx); c.setAttribute("aria-selected", String(i === AC.idx)); });
  $("h-input").setAttribute("aria-activedescendant", `h-ac-${AC.idx}`);
}
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

// Fork with roots (M9 UX fix): the popover's "fork to change them" copy was a
// dead end — the Fork button posted an empty body, so a fork always inherited
// the parent's roots and the fence could never move. The fork route already
// accepts roots (narrow-only, enforced server-side in session.fork); this is
// the picker for it. Widening stays a NEW session, never a fork (ADR-0015).
function forkPopover() {
  closeForkPopover();
  if (!H.sid) return;
  const parentRoots = [...(H.roots || [])];
  let roots = [...parentRoots];
  const pop = document.createElement("div");
  pop.id = "h-fork-pop";
  pop.className = "st-card harness-popover harness-ws-pop";
  pop.setAttribute("role", "dialog");
  pop.setAttribute("aria-label", "Fork this session");
  pop.innerHTML = `<div class="harness-popover__actions"><span class="card-title">Fork — trusted folders</span>` +
    `<button class="st-btn st-btn--icon" id="h-fork-close" title="Close" aria-label="Close">✕</button></div>` +
    `<ul class="harness-ws-rows" id="h-fork-rows"></ul>` +
    `<div class="harness-ws-add"><input class="st-input" id="h-fork-input" autocomplete="off"
        placeholder="Add a subfolder of the current roots…" aria-label="Add a trusted folder for the fork">
      <div class="harness-ws-suggest" id="h-fork-suggest" role="listbox" hidden></div></div>` +
    `<p class="muted small" id="h-fork-note">A fork may only narrow these folders. To widen them, start a new session instead.</p>` +
    `<div class="harness-popover__actions">` +
    `<button class="st-btn st-btn--primary" id="h-fork-go">Fork</button>` +
    `<button class="st-btn st-btn--secondary" id="h-fork-asis">Fork as-is</button></div>`;
  mountPopover(pop);
  const close = () => { closeForkPopover(); $("h-fork").focus(); };
  $("h-fork-close").onclick = close;
  pop.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });

  const rows = $("h-fork-rows");
  const insideParent = (p) => !parentRoots.length ||
    parentRoots.some((pr) => p === pr || p.startsWith(pr.replace(/\/+$/, "") + "/"));
  const render = () => {
    rows.innerHTML = "";
    if (!roots.length) {
      rows.innerHTML = `<li class="muted small">${parentRoots.length
        ? "no folders — the fork keeps the server default workspace"
        : "server default workspace"}</li>`;
      return;
    }
    for (const r of roots) {
      const li = document.createElement("li");
      li.innerHTML = `<code class="inline" title="${esc(r)}">${esc(r)}</code>` +
        ` <button class="st-btn st-btn--icon" title="Remove" aria-label="Remove ${esc(r)}">✕</button>`;
      li.querySelector("button").onclick = () => { roots = roots.filter((x) => x !== r); render(); };
      rows.appendChild(li);
    }
  };
  render();

  const input = $("h-fork-input"), suggest = $("h-fork-suggest"), note = $("h-fork-note");
  wireRootsBrowse(input, suggest);
  input.onkeydown = ((prev) => (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      const v = input.value.trim().replace(/\/+$/, "");
      if (!v) return;
      if (!insideParent(v)) {
        note.textContent = `"${v}" is outside the parent's roots — a fork can only narrow them.`;
        return;
      }
      if (!roots.includes(v)) roots.push(v);
      input.value = ""; suggest.hidden = true;
      render();
    } else prev(e);
  })(input.onkeydown);

  const doFork = async (body) => {
    try {
      const {id} = await hpost(`harness/session/${H.sid}/fork`, body);
      close();
      toast("success", "Forked", `new session ${id}`);
      await loadSessions();
      openSession(id);
    } catch (e) { toast("error", "Fork", e.message); }
  };
  $("h-fork-asis").onclick = () => doFork({});
  $("h-fork-go").onclick = () => {
    const same = roots.length === parentRoots.length && roots.every((r) => parentRoots.includes(r));
    doFork(same ? {} : {roots});
  };
}
function closeForkPopover() {
  const pop = document.getElementById("h-fork-pop");
  if (pop) { unmountPopover(pop); pop.remove(); }
}
$("h-fork").onclick = () => { if (H.sid) forkPopover(); };

// settings popover (M10 crowding relief): the approvals/preset/plugins cluster
// moves INTO the popover on open and back into its bar slot on close. The
// nodes keep their identity, so every existing handler (policy change, preset
// change, manage buttons) stays bound — no re-wiring, no duplicated controls.
function settingsPopover() {
  closeSettingsPopover();
  const pop = document.createElement("div");
  pop.id = "h-settings-pop";
  pop.className = "st-card harness-popover harness-settings-pop";
  pop.setAttribute("role", "dialog");
  pop.setAttribute("aria-label", "Session settings");
  pop.innerHTML = `<div class="harness-popover__actions"><span class="card-title">Settings</span>` +
    `<button class="st-btn st-btn--icon" id="h-settings-close" title="Close" aria-label="Close">✕</button></div>` +
    `<div class="harness-settings-grid" id="h-settings-grid"></div>`;
  mountPopover(pop);
  const cluster = $("h-settings-cluster");
  const grid = $("h-settings-grid");
  while (cluster.firstChild) grid.appendChild(cluster.firstChild);
  cluster.hidden = true;
  $("h-settings").setAttribute("aria-expanded", "true");
  $("h-settings-close").onclick = () => { closeSettingsPopover(); $("h-settings").focus(); };
  pop.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeSettingsPopover(); $("h-settings").focus(); } });
}
function closeSettingsPopover() {
  const pop = document.getElementById("h-settings-pop");
  if (!pop) return;
  const cluster = $("h-settings-cluster");
  const grid = $("h-settings-grid");
  while (grid.firstChild) cluster.appendChild(grid.firstChild);
  unmountPopover(pop);
  pop.remove();
  $("h-settings").setAttribute("aria-expanded", "false");
}
$("h-settings").onclick = settingsPopover;
// the bar button stays a plain "Settings" (uniform with the other actions);
// a badge appears ONLY when something deviates from the defaults — state you
// must notice, silence when there is nothing to notice
function settingsSummary() {
  const mode = $("h-policy").value;
  const preset = $("h-preset").value || "default";
  const badge = $("h-settings-badge");
  const parts = [];
  if (mode !== "ask") parts.push(mode);
  if (preset !== "default") parts.push(preset);
  badge.hidden = parts.length === 0;
  badge.textContent = parts.join(" · ");
  $("h-settings").title = parts.length
    ? `Settings — currently: ${mode} · ${preset}`
    : "Approvals, preset and plugins for this session";
}

// approval policy: ask | never | allowlist | yolo (patterns ride the store;
// the MODE never does — yolo is per-session and never restored, ADR-0010)
$("h-policy").onchange = () => {
  $("h-allowlist").hidden = $("h-policy").value !== "allowlist";
  setYoloBanner($("h-policy").value === "yolo");
  settingsSummary();
  savePolicy();
};
$("h-allowlist").onchange = savePolicy;
function setYoloBanner(on) {
  $("h-yolo").hidden = !on;
  const pill = $("h-state");
  if (on && H.busy) pill.dataset.state = "yolo";
  else if (pill.dataset.state === "yolo") pill.dataset.state = H.busy ? "generating" : "idle";
}
async function savePolicy() {
  if (!H.sid) return;
  const pats = $("h-allowlist").value.split(",").map((p) => p.trim()).filter(Boolean);
  store.set("harness.allowlist", pats);
  try {
    await hpost(`harness/session/${H.sid}/policy`, {mode: $("h-policy").value, allowlist: pats});
    toast("success", "Approval policy", $("h-policy").value);
  } catch (e) { toast("error", "Policy", e.message); }
}

// ------------------------------------------------------------------ resizable sidebar (M7 D9)
(function resizer() {
  const sep = $("h-resizer");
  const layout = document.querySelector(".harness-layout");
  const clamp = (w) => Math.max(160, Math.min(480, w));
  const saved = store.get("harness.side", 240);
  layout.style.setProperty("--h-side", `${clamp(saved)}px`);
  let drag = null;
  sep.addEventListener("pointerdown", (e) => {
    drag = {x: e.clientX, w0: clamp(saved)};
    sep.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  sep.addEventListener("pointermove", (e) => {
    if (!drag) return;
    layout.style.setProperty("--h-side", `${clamp(drag.w0 + e.clientX - drag.x)}px`);
  });
  sep.addEventListener("pointerup", (e) => {
    if (!drag) return;
    store.set("harness.side", clamp(drag.w0 + e.clientX - drag.x));
    drag = null;
  });
  sep.addEventListener("keydown", (e) => {
    const cur = parseInt(getComputedStyle(layout).getPropertyValue("--h-side"), 10) || 240;
    if (e.key === "ArrowLeft") layout.style.setProperty("--h-side", `${clamp(cur - 16)}px`);
    else if (e.key === "ArrowRight") layout.style.setProperty("--h-side", `${clamp(cur + 16)}px`);
    else return;
    e.preventDefault();
    store.set("harness.side", parseInt(getComputedStyle(layout).getPropertyValue("--h-side"), 10));
  });
})();

// ------------------------------------------------------------------ start
(async function harnessBoot() {
  $("h-allowlist").value = store.get("harness.allowlist", []).join(", ");
  await loadPresets();
  if (await pluginsOn()) $("h-plugin-manage").hidden = false;   // ADR-0014: off -> no affordance
  pluginBadge();
  await loadSessions();
  setWsChip();                             // M9: the chip starts honest (no session = server default)
  settingsSummary();                       // M10: the Settings button carries the live state
  const saved = store.get("harness.session", null);
  if (saved && H.sessions.some((s) => s.id === saved)) openSession(saved);
  else if (H.sessions.length) openSession(H.sessions[0].id);
  else composerEnabled(false);
})();
