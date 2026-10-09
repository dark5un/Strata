# ADR-0007: plan and goal modes are soft guidance, log-only state

Date: 2026-10-09
Status: accepted

## Context

dsh's plan/goal subsystems (research/dsh-agent-core.md §5) enforce nothing by
themselves: plan mode is a prompt section plus an exit tool that asks the
human; goal mode is a durable snapshot plus a round driver that queues one
user message per idle point. Hard enforcement lives in orthogonal gates
(sandbox, approval policy). We adopt that posture.

## Decision

- State is durable events only: `plan/mode {active}` and
  `goal/change {objective, phase, maxRounds}`, folded from the log on every
  attach/resume/fork. No mode state outside the session log.
- The loop injects mode guidance as a system message via `system_sections`
  callables at derive time — never written into the log as messages.
- `plan_mode` and `goal` tools mutate only these events (ungated: they touch
  no machine state). `exit_plan_mode` presents the plan through the ask seam
  when a board is wired: leaving plan mode is a human decision.
- The goal round driver lives in the manager's `_drive` chain: at each idle
  point, queued followups first, then one goal-sourced `user/message` while
  phase=active. Rounds count from the log (`source: "goal"`); hitting
  maxRounds flips the goal to `blocked` durably. A restart or fork never
  auto-revives a goal: only a live driver chain queues rounds.
- Enforcement stays orthogonal: the approval policy and the workspace fence
  decide what the agent may do; plan mode only *asks it not to*, and the
  allowlist/policy UI is the hard lever.

## Consequences

- Modes survive reload/crash/fork exactly like everything else (ADR-0004).
- A model that ignores plan-mode guidance is still bound by approvals —
  soft mode cannot become a security boundary, by construction.
- The UI shows mode chips (Plan/Goal buttons, `is-on` state) driven by the
  same durable events, so any client renders the truth.
