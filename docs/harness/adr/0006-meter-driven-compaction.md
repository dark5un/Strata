# ADR-0006: meter-driven compaction at the step boundary

Date: 2026-10-09
Status: accepted

## Context

ADR-0004 made compaction possible (prune shadows, summarize brackets) and the
meter (ADR-0002: monitor JSON only) can estimate context fill, but nothing
*decided when* to compact. M2 left a turn-start prune in the manager, which
does not help long turns: the window fills mid-turn, exactly when tool spam
accumulates.

## Decision

The loop owns the trigger. At every step boundary (`_maybe_compact`):

1. fold the meter from the session log,
2. read `engine.max_context` through the manager's `metrics_fn` (the
   Service.metrics() callable — never engine internals, ADR-0002),
3. if pressure >= `compact_at` (default 0.80): append a durable
   `context/pressure` event, run `prune_tool_results` (free, no model call),
   re-fold, and if still over (or nothing was pruned) run
   `summarize_oldest` (one model call).

The step boundary is deliberate: the surface is balanced there (no
half-open tool pair), so span edges never split a call from its result.
A monitor read that throws is ignored — compaction is best-effort, the
request itself is not blocked on metrics.

## Consequences

- Long turns survive context pressure without user intervention.
- The 0.80 threshold leaves headroom for the next request's prompt growth
  plus the summary call itself.
- `context/pressure` makes every automatic compaction auditable in the log
  and visible in the tab.
