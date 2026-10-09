# ADR-0002: No C++ — the harness speaks HTTP to its own process

Date: 2026-10-09
Status: accepted

## Context
Strata is a C++/CUDA engine behind a Python serve layer. A harness could in
principle reach into the engine (slots, KV cache, expert streaming) for tighter
integration. The user's call: "we probably not going to go deep into cpp, we
can just use the endpoints provided."

## Decision
The harness never links against or forks engine internals. It consumes:
- the in-process `Service` object (same process as the HTTP server) for
  request dispatch — no loopback HTTP hop for the agent's own completions;
- `Service.metrics()` (server.py:2845) and `v1_status()` for engine awareness:
  context fill, tok/s, queued, slots, conversation-cache reuse — used to time
  compaction and to surface live state in the tab;
- the existing chat-template/tool-call layer in `serve/frontend.py` unchanged:
  the harness sends OpenAI-shaped requests with `tools`, and frontend.py parses
  `<tool_call>` XML back into structured calls.

## Consequences
- Upstreamable: zero engine risk; reviewers see Python-only changes.
- Engine awareness is read-only and bounded by what /metrics already exposes.
- If a future need exceeds the endpoints (e.g. slot save/restore for session
  fork at the KV level), that is a new ADR + an upstream feature request, not
  a fork hack.
