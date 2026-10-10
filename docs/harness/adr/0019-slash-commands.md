# ADR-0019: Slash commands — client-side dispatch over existing routes

Date: 2026-10-10
Status: accepted

## Context

The parity audit lists slash commands as missing; the user chose the
Hermes format with a Tier-1 starter set tuned to our work
(research/hermes-slash-commands.md, m11-parity-plan.md thread 3).
Design pass: docs/harness/m11c-commands-ux-design.md.

## Decision

1. **Client-side dispatch, zero new command surface.** The composer
   parses `/name args` and calls the same routes the buttons call.
   Commands never reach the model and are never logged as events:
   the log stays the session's truth (ADR-0004); a command is a
   keyboard shortcut, not a turn. Command replies render as local
   transcript notes (ephemeral across reload).

2. **Two new backend routes, both general (not command-shaped):**
   `POST /harness/session/{sid}/title` (append a user session/title
   row — the fold already prefers the latest title) and
   `POST /harness/session/{sid}/compact` (manual compaction, idle
   only — the meter-driven trigger stays the default). /compact and
   /title are the only commands needing new plumbing; everything
   else maps to existing routes.

3. **Skill invocation keeps progressive disclosure.** `/<skill-name>`
   sends a normal user message instructing the model to pull the
   body via skill_view — the store is never inlined into the request
   by the client. Built-in command names win collisions (Hermes
   rule). Plugin-registered commands: out of scope; the hook point
   would be the same registry map, fed from active_plugin_tools().

4. **A leading `/` is never a task.** Unknown slash words toast
   "unknown command" and send nothing; the escape hatch for a task
   that must start with `/` is a leading space (command detection
   trims, the message keeps the text).

5. **No confirm on /new** (differs from Hermes): sessions are
   durable logs — nothing is destroyed, the old one stays in the
   sidebar. Destructive-confirm stays reserved for what is actually
   destructive (session delete, already two-step inline).

6. **Tier 2 deferred with a reason:** /steer and /queue — the
   composer already auto-steers when busy (target:"steer"); explicit
   commands land only with a UI affordance worth parser cost.
   /model — the model is fixed per server instance. /goal /loop
   /bg /review — automation machinery, separate milestone if ever.

## Consequences

- The command registry lives in harness.js (name, help line,
  handler); /help renders from it; autocomplete filters it plus the
  skill names from GET /harness/skills.
- Commands are tab-only (the CLI driver has no composer) — fine:
  this surface is the browser tab.
