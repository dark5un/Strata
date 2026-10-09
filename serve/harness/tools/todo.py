"""serve/harness/tools/todo.py — a per-session task list the model maintains.

State lives in the session log (todo/change events); the tool only validates
and appends. The tab renders it as a checklist. Read/write of the list is
in-process, ungated (it mutates nothing on the machine).
"""
from __future__ import annotations

from ..loop import ToolDefinition

VALID_STATES = ("pending", "in_progress", "done", "cancelled")


def fold_todo(rows: list[dict]) -> list[dict]:
    """Replay todo/change events into the current list."""
    items: list[dict] = []
    for row in rows:
        if row["type"] == "todo/change":
            items = row["data"]["items"]
    return items


def todo_tool() -> ToolDefinition:
    def execute(args: dict, ctx) -> dict:
        items = args.get("items")
        if items is None:
            return {"text": "current todo:\n" + _render(fold_todo(ctx.session.events))}
        if not isinstance(items, list):
            return {"text": "items must be a list", "is_error": True}
        for it in items:
            if not isinstance(it, dict) or "content" not in it or it.get("status") not in VALID_STATES:
                return {"text": f"bad item {it!r}: need content + status in {VALID_STATES}", "is_error": True}
        ctx.session.append("todo/change", {"items": items})
        return {"text": "todo updated:\n" + _render(items)}

    return ToolDefinition(
        name="todo",
        description="Maintain the session task list. Call with items=[{content,status}] to replace it, "
                    "or with no arguments to read it. status: pending|in_progress|done|cancelled.",
        parameters={"type": "object", "properties": {
            "items": {"type": "array", "items": {"type": "object", "properties": {
                "content": {"type": "string"},
                "status": {"type": "string", "enum": list(VALID_STATES)}}}}}},
        execute=execute, concurrency_safe=True)


def _render(items: list[dict]) -> str:
    if not items:
        return "(empty)"
    marks = {"pending": "[ ]", "in_progress": "[>]", "done": "[x]", "cancelled": "[-]"}
    return "\n".join(f"{marks.get(str(i.get('status')), '[?]')} {i.get('content')}" for i in items)
