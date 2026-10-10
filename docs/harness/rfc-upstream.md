# RFC: a native agent "Harness" tab in the Strata web app

Draft for the upstream feature-request issue (Niko1221/Strata). Written in the
repo's docs style: plain words, measured numbers with what they were measured
on, no claims without a measurement.

---

## What do you want to do?

Strata today serves the model to *other* agents — Claude Code, Codex, Hermes,
MCP clients all point at `/v1`. But the Strata app itself has no agent: to run
a tool-using, multi-turn job on your own PC you still need a third-party
harness installed next to it.

I would like Strata to offer an optional fourth tab — **Harness** — a native
agent loop that runs against the local server in-process:

- multi-turn ReAct loop with tool calls (read/write/edit files, search, shell),
  streamed live to the tab;
- sessions that survive browser reloads: append-only JSONL event log, resume,
  fork, export (markdown/JSONL);
- approvals: every dangerous tool call is gated fail-closed (allow/deny/ask)
  with a durable policy event — the tab never runs a shell command without a
  decision;
- context management sized for local models: token meter driven by the
  monitor's own numbers, compaction that shadows old spans instead of
  rewriting them, spill files for oversized tool output;
- plan mode, goal mode, mid-turn steering, and subagents (child sessions with
  depth/budget caps);
- per-session trusted workspaces: each session is fenced to operator- or
  user-picked folders; forks may only narrow the fence (ADR-0015);
- agent presets (YAML: persona + tool allow/deny, bundled + user-editable),
  MCP tools in the registry, and self-authored plugins behind a sha256
  review gate — a plugin file is imported only after a human reviews its
  source and hash (ADR-0014);
- monitor awareness: the context-fill gauge and slot state come from
  `Service.metrics()` — the harness compacts before the engine 400s.

Goal, not implementation: "run an agent on your own PC without installing
another tool". The workaround I use today is pointing an external harness at
`127.0.0.1:8080/v1`, which works but keeps sessions, approvals and context
management outside Strata's view.

## Why this fits the project

- It answers demand that already shows in issues: agent workloads on Strata
  (#980 field report, #1431 tool-call recovery in agent workloads, #1615
  harnesses recognizing context-overflow, #728/#694 agentic-loop pain).
  A first-party harness can handle overflow and retry policy where the server
  actually knows the context state.
- It is additive and opt-in: `--harness` flag / `"harness": true` in the
  config, off by default. No changes to the engine, the C++ layer, the
  template layer, or any existing route. The fork boundary is
  `serve/harness/` (Python, stdlib only) + `serve/web/harness.js|css` +
  small hooks at the tail of the routes table and the tab list.
- It matches the UI philosophy: vanilla ES2020 JS, no build step, reuses
  tokens.css/components.css/st-* classes and the sprite icons.
- Security inherits the existing model: loopback by default, `_own_page`
  gates on control routes, approvals default to 'ask', keys read from env or
  run-config, never argv. Nothing is exposed beyond 127.0.0.1 without
  `--api-key`.

## Status: it exists and is tested

I built this as a fork ("strata-harness"), current against upstream 0.1.41
(fb58e0d):

- ~9,200 lines of code added across 70 files (+11,076 total incl. docs,
  2 deleted lines — both the tab-list arrays in app.js); 33 commits on the
  `harness` branch;
- 198 harness tests + the full upstream suite green under
  `python -m unittest`, plus ruff, pyright (strict on `serve/harness/`) and
  eslint gates (`tools/test_harness.sh`);
- MockEngine-based tests — no GPU needed, same style as `test_server.py`;
- ADRs (16) and UX design/review docs for each UI milestone;
- a security audit of the fork (tracked files, full git history, key
  handling) before any push;
- running in production on my box (RTX 5090 + 4070 Ti quadlets) since
  2026-10-10: sessions, approvals, per-session trusted folders, plugins
  behind a sha256 review gate (ADR-0014), presets, MCP tools.

## Proposed PR series (small, reviewable)

Cut as stacked branches off upstream/main (all current against fb58e0d):

1. **pr/1-scaffold**: `serve/harness/` package skeleton, `--harness` flag,
   routes table tail + hidden tab button, ADRs, lint/test configs,
   `tools/test_harness.sh`. No behaviour when the flag is off.
2. **pr/2-core-loop**: events/session/loop/tools modules with tests,
   CLI driver only (no UI).
3. **pr/3-tab**: harness.js/css, SSE stream, approvals UI, monitor gauge.
4. **pr/4-context**: meter, compaction, spill, plan/goal modes, steering.
5. **pr/5-subagents**: children, depth/budget caps, subagent badges.
6. **pr/6-polish**: presets editor, export, keyboard nav, session UX.
7. **pr/7-m7**: ask-user cards, full approval payloads, resizable sidebar.
8. **pr/8-plugins**: plugin loader + sha256 review gate + Plugins panel.
9. **pr/9-workspaces**: per-session trusted folders, roots picker,
   context pill, operator default roots (ADR-0015).

A follow-up slice for the M10 UX pass (popover focus stack, session
delete, uniform bar) is cut on request. Each slice is independently
testable and keeps the tab hidden until the previous one lands. Happy to
re-cut the series however review prefers, and to follow whatever
contribution process you want for a feature this size — I could not find
one documented, so this issue is my RFC.

## Extra notes

- MIT-licensed fork; I would contribute the whole thing under Strata's MIT
  license.
- The name "harness" is used here in the agent sense (the thing around the
  model), which collides with #1615's "harnesses" (external clients). Easy to
  rename the tab if that reads badly.
