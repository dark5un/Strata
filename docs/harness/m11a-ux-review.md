# M11a — Observability drawer: rendered UX review

Date: 2026-10-10. Method per docs/harness/viewport-matrix.md: DOM
assertions for all text (vision garbles small UI text), vision only
for layout/balance/contrast. Mock server :8197, one session driven to
3 turns + 1 approval over HTTP, drawer opened from the bar.

## Functional DOM assertions (all PASS)

- Stats button opens `#h-stats-pop` through mountPopover: scrim live,
  aria-label "Session stats", aria-expanded toggled.
- Turn rows: "#1 completed 2 steps · 1 tool (1 ok) · 1 asked · 1
  allowed · 0 denied · 0.1s 57.0%" — counts match the server fold
  exactly (single source of truth: the ctx column equals the pill's
  meter fold).
- Sparkline renders once >=2 turns (correctly absent at 1 turn),
  polyline + aria-label "context 25% to 57%, now 25%".
- Tool expand `<details>` present per turn; outcome badges use the
  design system's real variants (reading/queued/error — the draft's
  --ok/--warn do not exist; caught before ship).
- Escape on the popover node closes exactly it (scrim gone, focus
  back to Stats).
- Live tick: with the drawer open, a composer-driven turn added row
  #3 without any manual refresh (paint()'s turn/end hook).
- Session switch closes the drawer (no cross-session stats leak).
- `_t0` internal leaked into the JSON on the first live probe —
  fixed (pop before emit) + route test asserts clean keys.
- Copy: singular/plural fixed after the first render ("1 tool calls"
  -> "1 tool call").

## Viewport matrix (drawer open, geometry via getBoundingClientRect)

| width | pop w | overflow | notes |
|---|---|---|---|
| 1920x1080 | 560 | none | anchored under the bar group, right-aligned |
| 1440x900  | 560 | none | |
| 1001x900  | 560 | none | above the 1000px breakpoint |
| 999x900   | 560 | none | below it — sidebar collapse does not squeeze the drawer |
| 640x900   | 616 | none | full-width rule engages; rows wrap cleanly, % column stays pinned |

max-height 70vh engages with overflow-y auto when content grows;
3-turn content never scrolls at these heights.

## Vision verdicts

- 1280: popover clearly elevated (lighter surface + shadow); veil
  subtle at this content height but present (pixel-confirmed in the
  640 capture: "dimmed and slightly blurred behind it"). Layout
  clean: title -> rows -> sparkline -> totals, even rhythm, % column
  aligned.
- 640: fits without clipping; dense row wraps its details sub-line
  under the pill, not into the % column; no overlap anywhere.
  Minor cosmetic: flush-left at 640 (bar-anchored, same as every
  other popover) — consistent, not fixed.
- SHOULD (not applied): secondary metric text is low-ish contrast —
  it reuses the app's own `muted small` convention, identical to the
  transcript's quiet rows; changing it here would break uniformity
  (ADR-0016). Deliberate.

## BLOCKERs: none. Milestone closed.
