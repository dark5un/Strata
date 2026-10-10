# dsh feature-parity audit (M11)

Date: 2026-10-10. Method: every dsh subsystem from
research/dsh-agent-core.md + dsh-web-surface.md checked against our
serve/harness/ + serve/web/harness.js (grep-verified, not remembered).
Status: = present, ~ partial, ✕ missing. "Ours" notes where the
architecture differs on purpose (ADR references).

## Agent core

| dsh feature | us | ours / gap note |
|---|---|---|
| Event-sourced log, derived history (ADR-0004) | ✓ | JSONL per session; deriveMessages equivalent |
| Turn/step/tool event taxonomy | ✓ | events.py turn/step/tool/approval events |
| request/header full snapshot per model call | ✕ | no epoch/header event; replay & cache-invalidation reasoning impossible — cheap to add, high forensic value |
| Synthetic closers for orphaned tool calls (ABORTED_BEFORE_DISPATCH etc.) | ~ | interrupted closers exist (session.py); no per-outcome taxonomy (OUTCOME_UNKNOWN vs NOT_STARTED) |
| surfaceOp replace compaction (shadow span, lossless raw) | ✓ | compaction.py replace/shadowed_seq |
| compaction bracket as durable lock | ~ | compaction runs inline; no crash-detectable start/end bracket |
| Two-list inbox (next-turn/next-step) + steer/followup presets | ✓ | inbox.py send(target, preset); routes expose target=auto\|steer |
| conclude_turn data-driven stop | ✓ | loop.py |
| Hook waterfalls (pre_step/pre_tool/post_tool) | ✓ | ADR-0013; plugins ride them |
| Cooperative cancel + cascade (ADR-0008) | ✓ | |
| Token meter anchored on provider usage | ✓ | meter.py fold; pill reads the same number |
| Spill with opaque locator | ✓ | spill.py |
| Fail-closed approval vocabulary + durable policy | ✓ | approval.py; ADR-0010 |
| ask_user_question tool (options, multiSelect) | ✓ | tools/ask.py |
| todo tool | ✓ | tools/todo.py |
| Goal / plan soft modes (ADR-0007) | ✓ | modes.py + bar buttons |
| Subagents in-process, fork-seeded (ADR-0008) | ✓ | subagents.py; steer-to-child: ~ (one send path, no UI affordance) |
| Presets YAML (ADR-0009/0011) | ✓ | presets.py + editor UI |
| MCP tools in registry (ADR-0012) | ✓ | tools/mcp.py |
| Self-authored plugins + review gates (ADR-0014) | ✓ code, ✕ enabled | flag off on both servers — M11 thread 1 |
| Per-session trusted workspaces (ADR-0015) | ✓ | fork narrow-only |
| System prompt as surface node, in-history update (KV-cache friendly) | ✕ | we rebuild system prompt per request; Strata's prompt cache makes this the most expensive dsh idea we skipped |
| Assistant stream records embedded in settlement (TTFT/replay) | ~ | streaming events exist; no timed delta records in the log |
| Session rename / title set | ✕ | no route, no UI (sidebar icon is origin, not pencil) |

## Web surface

| dsh feature | us | note |
|---|---|---|
| Session list + badges (sub/preset/ws/running) | ✓ | |
| Session delete | ✓ | M10 |
| Session search/filter | ~ | filter exists in sidebar? (9 hits) — verify depth |
| Observability view (per-turn tool telemetry, timeline) | ✕ | THE known gap: events + meter have the data, zero rendered view |
| Context gauge | ✓ | pill (context % / tokens) |
| Usage/cost per session | ✕ | no /usage route; meter data is per-session, just not surfaced beyond the pill |
| Slash commands | ✕ | M11 thread 3; starter set chosen in research/hermes-slash-commands.md |
| Skills (store + progressive disclosure + /name) | ✕ | M11 thread 3 |
| Fork with roots picker | ✓ | M9 |
| Export md/jsonl | ✓ | |
| Approval cards inline | ✓ | full payload, M7a-2 |
| Subagent tree view | ~ | rows show "sub" badge + parent title; no tree |
| Plugin panel (review/disable/source diff) | ✓ | hidden while server flag off |
| Resume after reload | ✓ | |
| Multi-tab/live streams (WS/SSE) | ~ | polling; dsh uses WS mux — fine at our scale, note only |

## Milestone split (proposed)

M11a — Observability (the headline gap):
  - route: GET harness/session/{sid}/stats — per-turn: tool calls,
    tokens (meter fold), approvals asked/granted/denied, duration
  - UI: a drawer on the session (not a new tab — uniformity rule,
    ADR-0016): turn timeline list, subagent tree, context curve
  - data is already in the log; this is read + render only

M11b — Session basics the audit surfaced:
  - rename (route + inline edit on row, two-step like delete confirm)
  - /usage numbers in the observability drawer (same fold)
  - orphaned-tool taxonomy (ABORTED_BEFORE_DISPATCH / OUTCOME_UNKNOWN)
  - request/header snapshot event (forensics + future replay)

M11c — Skills + slash commands (design in m11-parity-plan.md thread 3;
  Tier-1 command list in research/hermes-slash-commands.md)

Deferred with reasons:
  - system-prompt surface node / in-history update: needs Strata
    prompt-cache semantics study; biggest perf win but biggest design
    cost — separate ADR before any code
  - WS mux: polling adequate locally
  - compaction durable lock: single-writer local store, low value
  - stream records in settlement: only if observability wants TTFT
