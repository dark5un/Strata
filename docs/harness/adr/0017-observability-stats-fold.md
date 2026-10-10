# ADR-0017: Observability via a server-side stats fold

Date: 2026-10-10
Status: accepted

## Context

The parity audit (docs/harness/parity-audit.md) ranked the missing
observability view as the headline dsh gap. All the data is already in
the session log (turn/step brackets, tool call/result pairs with
outcomes, approval asked/decided, compaction brackets, usage anchors,
CONTEXT_PRESSURE rows) — nothing needs capturing; the gap is read +
render. Design pass: docs/harness/m11a-ux-design.md.

## Decision

1. **Server-side fold, one code path.** A pure function
   `stats.fold_stats(rows, metrics_fn)` in `serve/harness/stats.py`
   walks the event rows and returns per-turn aggregates + session
   totals (schema in m11a-ux-design.md). The route
   `GET /harness/session/{sid}/stats` reads the session snapshot and
   folds. The client renders; it never re-derives numbers — same
   single-source-of-truth rule as the context pill (the meter fold).

2. **Context numbers ride the meter fold.** Per-turn ctx% is the
   `fold_meter` estimate at the turn's end seq, pressure via
   `context_pressure(metrics_fn(), est)` — identical to what the pill
   and compaction use. A turn with no usage anchor yet prices the
   whole surface (fold_meter's documented fallback), so early turns
   are estimates, not zeros.

3. **Tool outcome taxonomy comes from the log, not the UI.** A tool
   is counted per its TOOL_RESULT: `is_error` -> error; a synthetic
   outcome (TOOL_NOT_STARTED / ABORTED_BEFORE_DISPATCH /
   TOOL_OUTCOME_UNKNOWN) -> unknown; else ok. A TOOL_CALL with no
   result inside the turn (session still open on it) counts as
   `pending`. Approvals fold from approval/asked + approval/decided
   pairs by call_id: asked counts human-asked only (`asked: true` on
   decided); yolo/policy decisions fold as `auto` so the row never
   claims a human decided what no human saw.

4. **Drawer, not tab; popover, not panel.** Entry is a labeled
   `Stats` text button in the bar's action cluster, opening through
   `mountPopover()` (ADR-0016 §1–3). No new layout axis, no
   transcript squeeze, uniformity preserved.

5. **No auto-refresh loop.** The drawer renders on open and
   re-renders on a TURN_END row arriving through the transcript's
   existing poll pump while the drawer is open. One pump, two
   consumers.

6. **Unknown events stay unknown.** Readers ignore event types they
   do not know (ADR-0004 forward compat); the fold counts nothing for
   M11b's future request/header event until it is taught to.

## Consequences

- Stats are exact for closed turns and live-approximate for the open
  one (the fold includes an in-progress turn with `open: true`).
- Cost/currency stays out: tokens only, no price table.
- The fold is O(rows) per call; sessions are local and bounded by
  compaction — no caching layer until a profile says otherwise.
