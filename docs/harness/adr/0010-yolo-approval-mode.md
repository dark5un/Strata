# ADR-0010: yolo is a per-session approval mode, matched on full arguments

Date: 2026-10-09
Status: accepted

## Context

The approval vocabulary was `ask | never | allowlist`, where `never` means
deny-unless-allowlisted — there was no allow-everything mode (the user asked
for a "yolo mode"). Two hazards ride the current design:

1. `ApprovalRequest.args_preview` is a 200-char truncation (loop.py
   `_preview`), and the allowlist matches against that preview — a pattern
   can match text the human never saw, or miss the real arguments.
2. A mode chosen in the browser is durable via `approval/policy` events, so
   an aggressive mode silently survives restarts.

## Decision

- Add mode `yolo` to `POLICIES`: every gated tool auto-allows, no human ask.
- Every yolo run is still audited: a durable `approval/decided` row with
  `{outcome: "yolo", asked: false}` lands before the tool executes. The
  outcome vocabulary gains `yolo` (auto-allow); `allow-once` stays the
  human answer.
- Allowlist matching and preview rendering move to the FULL arguments:
  `ApprovalRequest` carries `args` (the dict) and derives its preview from
  them; `_match` runs on a canonical full-arguments string
  (`tool:command` for shell, `tool:path` for file tools, else the JSON).
  The truncated 200-char preview is a display fallback only, never a
  matching input.
- Yolo never persists across a restart: `fold_policy` maps a durable
  `mode: "yolo"` back to `ask` when a session is re-attached. The durable
  event records what was on; the attach decides what is on now. The browser
  store keeps allowlist patterns but never a mode.
- Yolo does NOT touch the sandbox: workspace-root confinement, step caps,
  tool timeouts and delegation budgets are loop invariants, not approval
  policy.
- The tab shows a persistent banner while yolo is on (loud, warn-tinted,
  part of the layout — a standing condition, not a toast).

## Consequences

- Fail-closed posture is unchanged: yolo is opt-in per session, explicit,
  visible, and fully logged; "no answerer configured" still denies.
- Allowlist patterns written before this change matched previews; after it
  they match full arguments. Patterns are fnmatch over `tool` or
  `tool:<canonical args>` — strictly more accurate, no silent widening of
  `never` (which still denies unless matched).
- The UI must render the full payload at decision time (M7 design D2):
  never approve what you cannot read.
