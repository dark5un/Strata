# ADR-0003: Server-side agent loop in Python; the tab is a thin client

Date: 2026-10-09
Status: accepted

## Context
dsh runs its agent loop in Node on the host and streams to a React client over
/api RPC + a WebSocket mux. Options for strata-harness: (a) loop in the
browser (JS), (b) loop in a sidecar process, (c) loop inside Strata's Python
server process.

## Decision
(c). The loop lives in `serve/harness/loop.py`, in the same ThreadingHTTPServer
process as the model server. The browser tab is a thin client:
- POST /harness/session, /harness/session/<id>/send, /harness/session/<id>/cancel,
  /harness/session/<id>/approval — JSON, same-origin, _own_page + _authorized
  gates like every other control route;
- GET /harness/session/<id>/events — SSE (typed `event:`/`data:` frames, the
  Anthropic-style writer already in server.py), seq numbers with gap detection,
  Last-Event-ID resume;
- tools (shell, fs, search) execute on the host under the approval policy.

## Consequences
- Sessions survive browser reload/close; multiple tabs observe one session.
- Tools can touch the filesystem — which a browser loop could not do at all,
  and which is the whole point of a harness.
- stdlib-only (threading, json, subprocess): matches upstream's dependency
  posture (requirements.txt is pinned and small; we add nothing).
- One process couples harness crashes to the model server: mitigated by the
  loop running in a per-session worker thread with a supervisor that settles
  the turn with an error event (fail-closed), never killing the server.
- Sidecar (b) would double the quadlet surface for no gain; browser (a) is a
  non-starter for host tools.
