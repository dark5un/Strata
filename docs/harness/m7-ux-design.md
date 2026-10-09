# M7 UX design pass — transcript honesty, observability, presets, yolo

Date: 2026-10-09. Inputs: the user's live 5090 session ("write me a small
javascript example" read as "garbage and random commands"), the M7a-M7d
entries in PLAN.md §5, and the standing transcript rules from m6-ux-design.

The bar is the Chat tab: it streams token by token, shows a Thinking block,
and its tool cards are the pattern the harness reuses. Where the harness
already matches (markdown bubbles, tool cards, decided-approval folds), M7
does not redesign — it closes the gaps that made the agent look unmotivated
and its decisions unreadable.

## D1 — Reasoning is painted, not lost (M7a-1)

EngineResult.reasoning exists today but never reaches the log or the DOM.

- Durable: `assistant/message` gains a `reasoning` field (forward-compat;
  unknown fields are ignored by older folders). It is NOT fed back into the
  derived model history (token cost, and the Chat tab's server already
  strips reasoning-only turns); it exists for the human and for export.
- Tab: a collapsed `<details class="st-collapse think">` (the Chat tab's
  exact shape: thinking icon + "Thinking" title + chevron) painted directly
  above the assistant bubble / tool cards of the same step. Collapsed by
  default once settled — the "why" is available, not mandatory reading.
- While reasoning streams live (D3), the provisional thinking block is
  open; it closes when content lands (same auto-open/auto-close posture as
  the Chat tab, user-touched respected).
- Export: markdown gets a `<details><summary>Thinking</summary>` block,
  matching the Chat tab's own export.

## D2 — Approval at decision time: never approve what you cannot read (M7a-2)

The ask card today shows a 200-char mid-pipe preview. Changes:

- The synthetic `approval/ask` row carries the FULL arguments (process-local;
  the durable log already has them in `tool/call`). No cap on the command.
- shell: hero line = the command, in a scrollable `<pre>` (max-height,
  overflow auto), cut at line boundaries in the collapsed one-liner only.
- write_file / edit_file: hero line = the decision in prose —
  "Write `scratch/sse_demo/sse.test.mjs` — 3,488 chars" /
  "Edit `serve/harness/loop.py` — replace 1 occurrence" — with the content
  (or find/replace pair) behind a nested `<details>` below. The path is the
  headline; the payload is expandable.
- search/read (ungated) are unaffected.
- Allowlist matching runs on the full arguments, never the preview
  (ADR-0010; fixes the hazard where a pattern matched truncated text).

## D3 — Live streaming into the transcript (M7a-3)

Today the tab paints only settled durable rows: a 40s generation is dead
air. Design:

- The loop forwards engine `on_event` deltas to a process-local callback;
  the manager publishes synthetic rows `stream/delta` (seq -1):
  `{kind: "reasoning"|"content", text}`. Never logged, never replayed —
  a reconnecting client reconciles from the durable rows instead.
- The tab keeps one provisional assistant bubble + provisional thinking
  block at the transcript end, appending text as it lands (plain text,
  no markdown re-render per token; the durable row replaces it with the
  markdown bubble when it arrives).
- Finalize: the durable `assistant/message` removes the provisional nodes.
  A cancelled/errored turn also clears them (turn/end handler).
- Scroll: provisional appends stick to the bottom when the reader is at
  the bottom (same rule as tool results, M6 review).

## D4 — Decided approvals (M7a-4)

Already folded in 80b0e29. M7 verifies in the rendered pass that the fold's
expand now shows the FULL preview (it inherits D2's payload).

## D5 — Type hierarchy (M7a-5)

Audit bubble/label/code sizes against the Chat tab at the five viewport
widths (docs/harness/viewport-matrix.md). The Chat tab is the bar; harness
must not invent sizes. Measured, not eyeballed: getBoundingClientRect() +
computed font-size on `.st-bubble`, `.tool-call__name`, `.tool-call__pre`,
`.think-title` in both tabs.

## D6 — Yolo mode (M7d-2)

- Policy select gains "yolo (auto-allow, logged)".
- While yolo is on: a persistent banner between the bar and the transcript —
  warn-tinted, icon + "YOLO — tools run without asking. Every run is still
  logged." The state pill gets a warn dot. The banner is part of the tab,
  not a toast: the mode is a standing condition, not an event.
- Every auto-allowed tool still lands `approval/decided {outcome: "yolo",
  asked: false}` — the audit trail shows what ran unattended.
- Yolo is per-session and never restored on restart: `fold_policy` maps a
  durable yolo back to "ask" on attach (ADR-0010). The browser store keeps
  allowlist patterns but never a mode.
- The sandbox (workspace fence, step caps, depth budgets) is NOT part of
  yolo — stated in the banner tooltip.

## D7 — Preset manage panel (M7d-1)

- A gear button beside the preset select opens a popover (same pattern as
  the export/goal popovers): rows = name + badge (bundled | user).
- Actions per row: EDIT (user presets; bundled ones open EDIT as
  duplicate-to-user first), DUPLICATE (bundled -> user copy), DELETE (user
  only, confirm by second click on the same button). Plus NEW at the top.
- Editor form over the YAML schema: name (locked when editing), persona
  textarea, tools.allow/deny as checkbox pairs fed by GET /harness/tools
  (the live registry), description. The YAML file stays the source of
  truth; the UI is an editor, not a database (ADR-0011).
- Preset changes apply to NEW sessions; the current session changes by
  fork (already the documented rule — the panel says it once, in the
  popover footer, not per row).

## D8 — Observability rows are quiet (M7b)

Step timing + attempt events are log-side (forensics, not UI). The tab
paints nothing new for them except: a failed model call shows a note
("model call failed after 12.3s: ...") instead of silence, and the tool
card's running badge can carry elapsed ms in its tooltip. No new visual
language.

## D9 — Resizable sessions sidebar (user, 2026-10-09)

The 240px Sessions column is fixed today; long titles truncate and subagent
rows need room. Add a vertical drag separator between the sidebar and the
main column: pointer drag + Arrow keys on the separator (role="separator",
aria-orientation="vertical"), clamp 160-480px, width persisted in the
browser store (view preference, not session state). Grid becomes
`var(--h-side, 240px) 4px 1fr`.

## D10 — Session titles: a short model summary, not the raw first line (user, 2026-10-09)

"as they usually do in such tools": dsh/Hermes/Claude Code name sessions
with a 2-6 word summary of the ask. Today `_session_title` truncates the
first user message at 60 chars — fine as a fallback, not as the label.

- Durable event `session/title {text, by: "model"}` appended once, after
  the FIRST completed turn of a user-origin session (never for subagents,
  never regenerated — the human can rename later if we add that).
- Generation is one tiny engine call over the first user message (<=500
  chars): "Reply with a title for this conversation: 2-6 words, lowercase,
  no punctuation." Best-effort: any failure leaves the truncation fallback.
- `_session_title` prefers the durable title, falls back to the truncated
  first message (forks inherit the prefix, so the title rides along).
- The tab repaints the session list when the row lands (it streams like
  any durable event).

## States matrix

| state | bar | transcript | composer |
|---|---|---|---|
| generating (no deltas yet) | pill working | caret | Enter steers |
| streaming content | pill working | provisional bubble grows | Enter steers |
| pending approval | pill working | loud card, full payload | Enter steers |
| yolo on | pill working + banner | cards fold instantly (decided yolo) | Enter steers |
| reconnect | policy restored from /events | durable replay + pending asks | restored |

## a11y

- Banner: role="status", aria-live="polite" on entering yolo.
- Provisional bubble: aria-hidden while provisional (the durable row
  announces the same text on finalize; no double read).
- Preset popover: role="dialog", labelled; Esc returns focus to the gear.
- Approval hero line: the path/command is real text (selectable), not a
  title attribute.
