# ADR-0008: subagents are in-process child sessions

Date: 2026-10-09
Status: accepted

## Context

dsh models subagents as a runtime with pluggable backends (in-process spawn,
fork-in-process, SDK process, external adapters), with `origin:'subagent'` +
`delegationDepth` carried in the SessionHeader so the recursion budget
survives restarts (research/dsh-agent-core.md §5). strata-harness has no
process-multiplexing need: the manager already runs one worker thread per
session, sessions are event-sourced JSONL, and `session.create()` already
accepts `origin`, `parent_session`, `delegation_depth`, `agent_preset`
headers (seeded in M1, unused until now).

## Decision

Subagents are **child sessions in the same process**, spawned by a
`delegate` tool:

- The child is a normal session (own log, own Loop, own worker thread via
  `manager._drive`), header `origin="subagent"`, `parentSession=<parent id>`,
  `delegationDepth=parent+1`, `agentPreset` chosen by the caller.
- Budgets fold from the parent's log (restart-safe, dsh's rule): durable
  `subagent/spawn` events; max depth 2, max 4 children per session. A
  refusal is a tool-result error the model can read and adapt to.
- Settlement lands in the parent as a `user/message` with
  `source="subagent"` and `child=<id>` — the same shape the goal/steer
  drivers already use, so the parent's next derive sees it for free.
- Child approvals relay to the parent's stream (one place to answer);
  `decide` routes resolve against the parent's live set plus its children.
- Cancelling the parent cascades to running children.
- No fork-in-process seeding, no out-of-process backends: the fork tool
  already covers "seed a child with the parent's prefix" manually, and
  process isolation would break the single-writer + approval-channel model.

## Consequences

- Children are first-class in the UI (session list, resume, export) — no
  hidden runtime state.
- The manager owns the child lifecycle; the `delegate` tool is a thin
  adapter built per-session with a closure over the manager (same pattern
  as `spill_read_tool` over the SpillStore).
- Depth/child budgets are policy constants on the manager, not model-facing
  knobs; raising them is a code/config decision, not a prompt one.
