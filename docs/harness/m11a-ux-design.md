# M11a — Observability drawer: UX design pass

Date: 2026-10-10. Inputs: docs/harness/parity-audit.md (M11a row),
ADR-0016 (bar uniformity, popover stack), ADR-0004 (the log IS the
session), ADR-0005 (no build step — vanilla JS only).

## What the drawer answers

Three questions, in the order a user asks them:

1. "What did this session actually do?" — turn timeline: per turn,
   steps, tool calls (names + outcome badges), approvals
   asked/granted/denied, wall time.
2. "How much context is it eating and why?" — context curve per turn
   (the meter fold, same number as the pill), plus the biggest
   tool-result payloads that landed since the last compaction.
3. "Is it healthy right now?" — live state: busy/idle, pending
   approval count, last step ms, subagent children.

The drawer is READ + RENDER only: no new capture, no new event types
(M11b adds the request/header event; the drawer will show it when it
exists, without needing to change now).

## Entry point (uniformity rule, ADR-0016 §2/§3)

A plain `st-btn st-btn--secondary` labeled **Stats** in the session
bar, in the action cluster (Settings · Stats · Export · Plan · Goal ·
Fork). No icon-only affordance, no badge — the drawer is a view, not a
state, so it stays silent (badges are for deviations).

It opens through `mountPopover()` like every other bar popover: scrim
stack, Escape closes exactly it, full labeled surface inside.

## Layout inside the popover

Width ~560px (fits the 640px viewport minus margins; at 640 the
popover goes full-width of `#h-bar` like the other popovers do).
Three stacked sections, each a labeled block (card-title + content):

### 1. Now (only when the session is live or has pending items)

One row: state pill text · pending approvals count · last step ms ·
children (subagent count). Silence when idle with nothing pending —
the transcript already says that.

### 2. Turns (the main list)

One row per TURN_START..TURN_END pair, newest last (chronological,
matching the transcript's reading direction):

```
#3  completed · 4 steps · 6 tools · 2 approvals · 38.2s   ctx 41%
    read_file ✓ · shell ✓ · shell ✕ · write_file ✓ ...
```

- Turn number, end reason badge (completed / error / cancelled /
  blocked / max_steps), step count, tool count with per-outcome
  badges (✓ ok, ✕ error, ⚠ unknown/aborted), approvals
  `asked→granted/denied` counts (silent when zero — silence at
  defaults), turn wall time.
- Right-aligned: context % at turn end (from the CONTEXT_PRESSURE row
  in the turn, else the meter fold at TURN_END seq) — this column IS
  the context curve in text form; a compact inline SVG sparkline sits
  under the list (one polyline of ctx% per turn, compaction turns get
  a marker tick). No chart library (ADR-0005).
- The tool-name line is a `<details>` expand: full list with tool
  name + outcome + spill badge + duration when STEP/TOOL rows carry
  it. Quiet fold rule (transcript design rules): the row is the scan
  layer, the expand is the read layer.
- Compaction turns render as their own row: `compaction · summary ·
  -12.4k tokens` (COMPACTION_START/END + the meter delta).

### 3. Totals (bottom strip)

Session totals: turns, steps, tool calls by outcome, approvals
granted/denied, total tokens (prompt+completion from the last usage
anchor), session wall time. Same fold function the per-turn list
uses — one code path, two aggregations.

## States

- No session open: Stats button disabled (same as Export/Fork).
- Session with zero turns: popover shows "No turns yet." — never an
  empty skeleton.
- Very long sessions (100+ turns): list capped at the last 50 turns
  with a "show all" toggle; the sparkline always covers all turns
  (it is cheap).
- Live session: the drawer does NOT auto-refresh on a timer; it
  re-renders when opened and when a TURN_END arrives while it is
  open (the transcript poll already delivers rows; the drawer hooks
  the same pump). No second poll loop.

## Data contract

`GET /harness/session/{sid}/stats` →
```json
{
  "turns": [{"n": 1, "reason": "completed", "startSeq": 3, "endSeq": 40,
             "ms": 38200, "steps": 4,
             "tools": {"ok": 5, "error": 1, "unknown": 0},
             "toolNames": [{"name": "shell", "outcome": "ok", "ms": 120}],
             "approvals": {"asked": 2, "granted": 1, "denied": 1},
             "ctxPct": 41.2, "ctxEstimate": 105000,
             "compaction": null}],
  "totals": {"turns": 12, "steps": 40, "tools": {...}, "approvals": {...},
             "tokens": {"prompt": ..., "completion": ...},
             "ms": ..., "ctxPct": ..., "ctxEstimate": ...},
  "children": [{"id": "...", "title": "...", "status": "..."}]
}
```

Server-side fold in a new `stats.py` (pure function over the event
rows — same fold discipline as meter.py; unit-testable without HTTP).
The route reads the session's snapshot rows and folds. ctxPct comes
from `context_pressure()` with the same monitor metrics the pill uses
— single source of truth (meter fold rule from the skill: never
re-derive client-side).

## A11y

- Stats button: `aria-haspopup="dialog"`, `aria-expanded` toggled
  (same as h-ws/h-settings).
- Turn rows: `<li>` in an ordered list; badges carry text
  (`ok`/`error`), not just glyphs — glyphs are `aria-hidden`.
- Sparkline: `role="img"` + `aria-label` with min/max/final ctx%.
- Popover gets `aria-label="Session stats"` and Escape on the node
  (ADR-0016 §5).

## Copy

Section titles: "Now" · "Turns" · "Totals". Turn badges read as
words: completed, error, cancelled, blocked, step-limit. Approvals:
"2 asked · 1 allowed · 1 denied". Context column header tooltip:
"context at end of turn (same number as the pill)".

## Explicit non-goals (M11a)

- No cost/currency (no price table; tokens only).
- No per-tool latency percentiles; raw ms per tool when present.
- No request/header forensics (M11b event; the drawer will pick it up
  as a turn-row expand line when the event exists).
- No subagent tree beyond the children strip (tree view is a later
  audit row).
