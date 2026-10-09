# M4 UX design pass — subagents, presets, export, keyboard nav

Status: design (pre-implementation, per the standing rule). 2026-10-09.
Applies to: serve/web/harness.js|css, the /harness routes it talks to.
Design system: tokens.css + components.css only; no new primitives (ADR-0001
fork boundary, M2 rule holds).

## 1. Flows

### 1.1 Subagents (delegate)

The model delegates; the human watches and can intervene.

- The agent calls `delegate(task, preset?)`. A child session starts
  (origin=subagent, parent=this session, depth=parent+1). The tool returns
  immediately: "child <id> started". The parent keeps working.
- When the child settles, its result lands in the parent as a
  `user/message` with `source:"subagent"` — painted as a distinct card
  (icon `i-experts`, "Subagent · <child id>" header, result body), NOT as a
  user bubble.
- Child sessions appear in the session list, indented under the parent with a
  small "sub" badge and the parent's id as tooltip. Clicking one opens it like
  any session (full transcript, own stream).
- Child approvals relay to the PARENT's stream: an approval card in the
  parent's transcript carries the child's call_id; deciding it routes to the
  child's channel. One place to answer, no tab-hunting.
- Budgets are visible, not silent: depth cap 2, children per session cap 4.
  A rejected delegate paints as a tool-result error (the model sees it and
  adapts); no UI surprise.
- Stop (cancel) cascades: cancelling the parent cancels its running children.
  The Stop button tooltip says so when children are live.

States the UI must handle: child running (badge "running" in list), child
settled, child denied-by-budget, child cancelled, parent reload while child
runs (child keeps running server-side; list + stream reconcile as usual).

### 1.2 Agent presets

- A `Preset` select in the session bar, populated from GET /harness/presets.
  It sets the preset for NEW sessions (the + button reads it); the current
  session's preset is shown read-only in its list row (badge) and in the bar
  as a disabled select value — changing presets mid-session is not offered
  (persona/tools are session identity; fork instead).
- Built-in `default` preset (today's tool set, no persona). User presets are
  YAML files in serve/harness/presets/.

### 1.3 Export

- `Export` button in the bar -> two-item popover (Markdown / JSONL), matching
  the goal-popover pattern (st-card, Esc closes, focus first item).
- Markdown: human-readable transcript (messages, tool calls collapsed to
  one-line summaries, approvals as notes). JSONL: the raw log (already
  sensitive — the popover note says "includes tool output").
- Download via an <a download> to GET /harness/export?session=…&format=…

### 1.4 Keyboard nav

- Session list: roving tabindex, ArrowUp/Down move focus, Home/End jump,
  Enter/Space opens. The list is a `role="list"` of buttons (already buttons).
- Composer: `/` when focus is on the transcript/body focuses the input
  (dsh-style, no conflict with typing in inputs).
- Popovers (goal, export): Esc closes and returns focus to the opener.

## 2. A11y

- Subagent cards: `role="listitem"`-ish grouping is overkill; a heading-3
  inside the card names the child session. aria-live stays on the status
  pill only (transcript is too chatty for live regions — M3 decision holds).
- Preset select: visible `<label for>` like the approvals select.
- Export popover: role="dialog", aria-label, focus trap-lite (focus first
  button, Esc closes, focus restored).
- Keyboard nav: focus ring comes from st-btn/st-input tokens; no custom
  outline suppression.
- Badges ("sub", "running") carry text, not color alone.

## 3. Dark theme audit (M4 acceptance item)

harness.css uses only --st-* tokens (verified at write time). The audit is a
browser pass: open the tab in dark theme with a live-ish session (mock
engine), screenshot, check: approval tint, popover shadow, subagent card,
badges, spill locator contrast. Findings -> fix in the same commit series.

## 4. Copy

- Subagent card header: "Subagent · {id}" / settled line: "finished" |
  "failed" | "cancelled".
- Budget refusal (model-facing tool text): "delegation budget reached
  (depth {n}/{max} or {k} children); do the work yourself or fork instead".
- Export note: "JSONL includes raw tool output — treat it as sensitive".
