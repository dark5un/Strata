# ADR-0013: loop hook surface (pre_tool / post_tool)

Date: 2026-10-09
Status: accepted

## Context

The loop hard-codes its policies as branches: approval mode checks, spill,
compaction, subagent budgets. dsh's strongest structural idea is the hook
waterfall — policies are observers registered around the step, not code in
the step. PLAN M7c-2 asks for the stdlib version so that future policies
(including any eventual plugin story) become plugins instead of loop edits.

## Decision

Add two hook lists to `Loop` (alongside the existing `pre_step`):

- `pre_tool: list[Callable[[Loop, str, dict], str | None]]` — runs in
  registration order BEFORE the approval gate. Returning a string denies the
  call with that reason (result `is_error`, text `blocked by hook: <reason>`);
  returning None passes. A throwing hook is ignored (fail-open for observers,
  fail-closed only for the approval path, which stays the human gate).
- `post_tool: list[Callable[[Loop, str, dict, dict], None]]` — observers,
  run after the result is appended; exceptions swallowed.

Order in `_dispatch`: unknown-tool check -> pre_tool hooks -> approval gate ->
run. A hook denial never reaches the human (no ask card): the transcript shows
a normal error result, so the UI needs no new vocabulary.

Hooks are process-local wiring (manager/CLI set them); they are NOT
serialised, NOT preset-configurable, and NOT a plugin loading mechanism.
Self-authored plugins remain deferred (PLAN M7c-3, security-relevant).

## Consequences

- The allowlist policy could later be expressed as a pre_tool hook; the
  approval broker stays the default path (durable asked/decided rows).
- Test surface: one hook denial test + one post_tool observer test in
  test_loop.py; no format-version bump (no new durable event types).
