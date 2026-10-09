"""serve/harness/subagents.py — in-process child sessions (ADR-0008).

A subagent is a normal session with header origin="subagent",
parentSession=<id>, delegationDepth=parent+1. The recursion budget folds
from the parent's durable subagent/spawn events, so it survives restarts
(dsh's rule). Settlement lands in the parent as a user/message with
source="subagent" — the same shape the goal/steer drivers already use.

This module owns the budget fold and the delegate tool; the manager owns
the child lifecycle (spawn, settle, cancel cascade).
"""
from __future__ import annotations

from . import events as ev
from .loop import ToolDefinition

MAX_DELEGATION_DEPTH = 2      # a session at depth >= 2 cannot delegate further
MAX_CHILDREN_PER_SESSION = 4  # spawn events per parent log (budget, restart-safe)


def fold_spawns(rows: list[dict]) -> list[dict]:
    """The parent's durable spawn records: [{child, preset, depth}, ...]."""
    return [r["data"] for r in rows if r["type"] == ev.SUBAGENT_SPAWN]


def budget_check(rows: list[dict], depth: int) -> str | None:
    """None when a new child is allowed; else the refusal reason (model-facing)."""
    if depth >= MAX_DELEGATION_DEPTH:
        return f"delegation budget reached (depth {depth}/{MAX_DELEGATION_DEPTH}); do the work yourself or fork instead"
    if len(fold_spawns(rows)) >= MAX_CHILDREN_PER_SESSION:
        return (f"delegation budget reached ({MAX_CHILDREN_PER_SESSION} children for this session); "
                "do the work yourself or fork instead")
    return None


def child_result_text(handle) -> str:
    """The settlement summary the parent reads: the child's last assistant
    message plus its final turn reason."""
    content = ""
    reason = "completed"
    for row in reversed(handle.events):
        if row["type"] == ev.ASSISTANT_MESSAGE and row["data"].get("content"):
            content = str(row["data"]["content"])
            break
    for row in reversed(handle.events):
        if row["type"] == ev.TURN_END:
            reason = str(row["data"].get("reason", "completed"))
            break
    head = f"[subagent {handle.header['id']} {reason}]"
    return f"{head}\n{content}" if content else head


def delegate_tool(spawn) -> ToolDefinition:
    """spawn(task: str, preset: str) -> (child_id | None, refusal | None).

    Built per-session by the manager (closure pattern, like spill_read_tool):
    the tool itself knows nothing about threads or other sessions.
    """
    def execute(args: dict, ctx) -> dict:
        task = str(args.get("task", "")).strip()
        if not task:
            return {"text": "delegate needs a task: {\"task\": \"...\", \"preset\": \"researcher\"?}", "is_error": True}
        preset = str(args.get("preset") or "default")
        child_id, refusal = spawn(task, preset)
        if child_id is None:
            return {"text": refusal, "is_error": True}
        return {"text": f"child session {child_id} started (preset {preset}); its result wakes you when it settles"}

    return ToolDefinition(
        name="delegate",
        description=("Spawn a subagent session that works on `task` in the background and reports back. "
                     "Use for self-contained research/heavy subtasks; the result arrives as a message. "
                     "Optional preset picks the child's persona and tool set."),
        parameters={
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "the full, self-contained task for the child"},
                "preset": {"type": "string", "description": "agent preset name (default: default)"},
            },
            "required": ["task"],
        },
        execute=execute,
        concurrency_safe=True,
        timeout_s=30.0,
        needs_approval=False,   # spawning changes no machine state; the child's own tools stay gated
    )
