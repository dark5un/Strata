# ADR-0012: MCP tools in the harness registry

Date: 2026-10-09
Status: accepted

## Context

Strata already speaks MCP (`serve/mcp.py`: `McpHub` owns every configured
server, offers tools as OpenAI function tools named `<server>__<tool>`, and
routes calls back). The Chat tab gets them via `run_with_mcp` in server.py.
The harness has no plugin surface at all: `ToolRegistry.register` is
programmatic only, and presets can allow/deny existing tools but cannot add
any (PLAN M7c). The user asked whether the harness supports plugins and will
create its own like dsh.

## Decision

1. **MCP is the plugin transport for now.** The harness registers the
   configured hub's tools into its own registry at session-attach time, as
   ordinary `ToolDefinition`s built from `hub.template_tools()` (name,
   description, inputSchema pass through unchanged).
2. **Every MCP tool is gated.** `needs_approval=True`, fail-closed: an
   out-of-process server can do anything the machine allows, and the harness
   cannot inspect it. The approval card shows the FULL arguments (M7a rule).
   The only escape is the user's own approval policy (allowlist/yolo), which
   is a deliberate, durable session choice.
3. **Calls go through `hub.call(name, args, cancel)`** — the hub's own
   timeout, result cap (`max_result_chars`), restart-on-crash and error-as-text
   semantics apply unchanged. `McpCancelled` maps to a cancelled tool result.
   The harness spill layer (loop `_result`) still sees the text, so oversized
   MCP output spills like any other tool output.
4. **Namespaces are the identity.** The `<server>__<tool>` name is the
   registry key: it survives preset allow/deny (ADR-0009 vocabulary covers
   all registry names), subagent intersection, and allowlist matching
   (`canonical_args` falls through to `tool:{json}` for MCP tools).
5. **Wiring**: `HarnessManager(..., mcp_hub=hub)`; server.py passes the same
   hub the Chat tab uses — one set of MCP processes per server, no second
   start. `GET /harness/tools` merges the hub's names so the preset editor
   can restrict them.
6. **No hot-reload**: the tool set is snapshotted at attach. A server that
   starts later is visible to NEW sessions (and to a fork), not mid-session.
   Keeps the request/header envelope honest (M7b: tools list is stable per
   derive).

## Consequences

- Cheap, safe, upstreamable: zero changes to serve/mcp.py, one additive
  constructor arg in the fork's server.py hook, one new harness module
  (`tools/mcp.py`).
- Self-authored in-process plugins stay DEFERRED (security-relevant; the
  user decides). If ever enabled they ride the loop-hook surface (ADR-0013)
  behind a load-time review, not this registry path.
- A session's tool list can differ between sessions on the same server
  (hub readiness at attach time); the request/header row records the truth
  per derive, so replay stays reconstructable.
