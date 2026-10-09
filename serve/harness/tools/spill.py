"""serve/harness/tools/spill.py — the spill locator tool (ADR-0004).

A spilled tool result keeps head/tail inline plus a spill:// locator. This tool
reads the full text back in chunks, so the model can page through an oversized
output without re-spilling it into context whole. Ungated: it reads the
harness's own spill store, not the machine.
"""
from __future__ import annotations

from ..loop import ToolDefinition
from ..spill import SpillStore

CHUNK_CHARS = 20_000


def spill_read_tool(store: SpillStore) -> ToolDefinition:
    def execute(args: dict, ctx) -> dict:
        locator = str(args.get("locator", ""))
        if not locator.startswith("spill://"):
            return {"text": "locator must be a spill:// reference from an earlier result", "is_error": True}
        try:
            offset = max(0, int(args.get("offset", 0)))
        except (TypeError, ValueError):
            return {"text": "offset must be a character index", "is_error": True}
        text = store.read(locator)
        if text is None:
            return {"text": f"no spill file for {locator} (pruned or wrong session)", "is_error": True}
        window = text[offset:offset + CHUNK_CHARS]
        end = offset + len(window)
        more = f" [{end}/{len(text)} chars; continue with offset={end}]" if end < len(text) else " [end]"
        return {"text": window + more}

    return ToolDefinition(
        name="spill_read",
        description="Page through a spilled (oversized) tool result. locator from the earlier "
                    "result's text; offset is a character index; returns ~20k chars per call.",
        parameters={"type": "object", "properties": {
            "locator": {"type": "string"},
            "offset": {"type": "number"}},
            "required": ["locator"]},
        execute=execute, concurrency_safe=True)
