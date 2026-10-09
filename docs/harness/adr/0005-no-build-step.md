# ADR-0005: No new build step — vanilla JS + stdlib Python; TS/React rejected

Date: 2026-10-09
Status: accepted

## Context
dsh's web frontend is React 18 + Vite + tsdown with a dual-face plugin module
system. Strata's UI is deliberately the opposite: plain JS served straight from
disk, no bundler, no framework, design-system CSS (tokens.css/components.css).
A harness tab could be built either way in the fork.

## Decision
Match Strata, not dsh:
- `serve/web/harness.js` is plain ES2020, served by the existing /web/* static
  handler (server.py:4230) with zero route or build changes.
- No TypeScript, no bundler, no npm dependency in the fork.
- Quality gates for JS are eslint (flat config, scoped to harness.js) and
  careful DOM code; the heavy correctness testing lives on the Python side
  (unittest), where the loop, sessions and protocol actually are.
- Python side stays stdlib-only (ADR-0003); dev tooling (ruff, pyright) is
  config + venv, never a runtime dependency.

## Consequences
- Upstream contribution stays reviewable: no new toolchain in a project whose
  selling point is "runs on a normal PC".
- The tab code is bigger and more manual than React would be; acceptable —
  Strata's own app.js (1080 lines) proves the pattern scales to this UI size.
- If the tab ever outgrows vanilla JS, revisit (supersede this ADR) with a
  pre-compiled, committed-asset approach — never a build step in setup.py.
