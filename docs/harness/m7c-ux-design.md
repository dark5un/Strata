# M7c UX design pass — plugin surface (MCP tools + loop hooks)

Date: 2026-10-09. Scope: M7c items 1-2 (MCP tools into the harness registry,
loop hook surface). Self-authored plugins stay DEFERRED (security-relevant,
user decides — ADR territory only if ever enabled).

## What the user sees

M7c is mostly plumbing, but it changes three visible surfaces. Each placement
below is a design decision, not an implementation accident.

### 1. MCP tools in the transcript (tool cards + approval cards)

- An MCP call renders as a normal tool card: the namespaced name
  (`<server>__<tool>`, e.g. `fake__echo`) is the card title — the namespace IS
  the provenance (which server runs it), so it stays in the title, not a badge.
- Approval card: the generic fallback already shows the FULL JSON arguments in
  a scrollable pre (M7a rule: never approve what you cannot read). Head line:
  "Run `fake__echo`?" plus a small `mcp` badge with the server name in the
  tooltip — the badge answers "is this a machine tool or an out-of-process
  server?" at decision time.
- Result label keeps the M6 vocabulary: "Result · … (N chars, M lines)".
  Hub-capped results ("[... truncated …]") are honest text, no extra UI.

### 2. MCP servers in the preset editor (GET /harness/tools)

- The preset editor's tool checkboxes must list MCP tools too, otherwise a
  preset cannot allow/deny them (ADR-0009's vocabulary covers all registry
  names). Names appear in the same sorted list; the bundled-vs-MCP distinction
  is visible from the `__` namespace. No new column, no new control.
- A server that has not started yet contributes no names (attach-time
  snapshot). The editor shows what the NEXT session can actually get.

### 3. Nothing else

Loop hooks (ADR-0013) are a developer surface: no new UI. A denied-by-hook
call lands as a normal tool/result error row, same styling as a denied
approval — the transcript never needs to know who said no.

## States

- No MCP servers configured: identical to today (zero visual change).
- Server configured but not ready at attach: its tools are absent from that
  session; a new session sees them. (No spinner: the tab does not poll the
  hub; the Chat tab's MCP panel already owns server-status display.)
- Server crashes mid-session: the hub restarts it on the next call
  (upstream behaviour); a call that fails becomes an "error: …" result text —
  the model reacts, the loop never breaks.

## a11y / design system

- Badge reuses `st-badge` (existing primitive); no new CSS class beyond
  harness-scoped modifiers if needed.
- Approval card focus order unchanged (Allow / Deny buttons as today).

## Copy

- Badge label: `mcp`. Tooltip: `MCP server "<name>" — runs out of process`.
- No new toasts.
