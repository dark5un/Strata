"""serve/harness/tools/mcp.py — MCP hub tools in the harness registry (ADR-0012).

The configured McpHub's tools become ordinary ToolDefinitions at attach time:
name/description/inputSchema pass through, every tool is gated
(needs_approval=True, fail-closed — an out-of-process server can do anything
the machine allows and the harness cannot inspect it). Calls go through
hub.call(), so the hub's timeout, result cap, restart-on-crash and
error-as-text semantics apply unchanged; McpCancelled maps to a cancelled
result so the loop's turn cancel reaches the server too.

The hub attribute on the definition is a marker for the approval card (the
tab shows an `mcp` badge); the loop never reads it.
"""
from __future__ import annotations

from ..loop import ToolDefinition, ToolRunContext


def mcp_tools(hub) -> list[ToolDefinition]:
    """The hub's ready tools as gated registry entries (snapshot at call time)."""
    out = []
    for t in hub.template_tools():
        out.append(_make(hub, t))
    return out


def _make(hub, spec: dict) -> ToolDefinition:
    from serve.mcp import McpCancelled

    name = spec["name"]

    def execute(args: dict, ctx: ToolRunContext) -> dict:
        try:
            r = hub.call(name, args if isinstance(args, dict) else {}, ctx.cancel)
        except McpCancelled:
            return {"text": "mcp call cancelled", "is_error": True}
        return {"text": r.get("text", ""), "is_error": not r.get("ok", False)}

    tool = ToolDefinition(
        name=name,
        description=str(spec.get("description") or ""),
        parameters=spec.get("parameters") or {"type": "object", "properties": {}},
        execute=execute,
        concurrency_safe=True,
        timeout_s=float(hub.settings["timeout_s"]) + 10.0,  # the hub answers first; this is the backstop
    )
    tool.hub = hub  # type: ignore[attr-defined]  # marker: the tab badges hub tools as `mcp`
    return tool
