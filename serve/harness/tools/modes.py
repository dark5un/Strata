"""serve/harness/tools/modes.py — plan/goal tools (ADR-0007).

These mutate only session state (a durable event), never the machine: they are
ungated. exit_plan_mode presents the plan through the ask seam when a board is
wired, so leaving plan mode is a human decision, not a model formality.
"""
from __future__ import annotations

from .. import events as ev
from ..loop import ToolDefinition
from ..modes import GOAL_PHASES


def plan_mode_tool(board=None) -> ToolDefinition:
    def execute(args: dict, ctx) -> dict:
        action = str(args.get("action", ""))
        if action == "enter":
            ctx.session.append(ev.PLAN_MODE, {"active": True})
            return {"text": "plan mode on: research and design only; finish with exit_plan_mode."}
        if action == "exit":
            plan = str(args.get("plan", "")).strip()
            if not plan:
                return {"text": "exit needs the plan text", "is_error": True}
            if board is not None:
                q = board.ask(f"Plan ready — approve and leave plan mode?\n\n{plan}",
                              [{"label": "approve"}, {"label": "reject"}], multi=False)
                q.answered.wait()
                ans = q.answer or {}
                chosen = ans.get("selected") or ans.get("text") or ""
                if "reject" in str(chosen).lower():
                    return {"text": "the user rejected the plan; stay in plan mode and revise",
                            "is_error": True}
            ctx.session.append(ev.PLAN_MODE, {"active": False, "plan": plan})
            return {"text": "plan mode off: the plan is approved, execute it."}
        return {"text": f"bad action {action!r}: enter|exit", "is_error": True}

    return ToolDefinition(
        name="plan_mode",
        description="Switch plan mode. action='enter' to start designing (no machine changes); "
                    "action='exit' with the full plan text to hand it to the user for approval.",
        parameters={"type": "object", "properties": {
            "action": {"type": "string", "enum": ["enter", "exit"]},
            "plan": {"type": "string", "description": "the plan, required for exit"}},
            "required": ["action"]},
        execute=execute, concurrency_safe=False)


def goal_tool() -> ToolDefinition:
    def execute(args: dict, ctx) -> dict:
        phase = str(args.get("phase", ""))
        if phase not in GOAL_PHASES:
            return {"text": f"bad phase {phase!r}: {'|'.join(GOAL_PHASES)}", "is_error": True}
        objective = str(args.get("objective", "")).strip()
        if not objective:
            goal = next((r["data"] for r in reversed(ctx.session.events) if r["type"] == ev.GOAL_CHANGE), None)
            objective = str((goal or {}).get("objective", ""))
            if not objective:
                return {"text": "no goal set: pass objective", "is_error": True}
        data = {"objective": objective, "phase": phase,
                "maxRounds": int(args.get("max_rounds", 8))}
        ctx.session.append(ev.GOAL_CHANGE, data)
        return {"text": f"goal {phase}: {objective}"}

    return ToolDefinition(
        name="goal",
        description="Set or update the session goal. phase=active starts/continues it (the harness "
                    "queues one round per idle point), complete ends it, paused holds it for the "
                    "user, blocked stops the driver until the human intervenes.",
        parameters={"type": "object", "properties": {
            "objective": {"type": "string"},
            "phase": {"type": "string", "enum": list(GOAL_PHASES)},
            "max_rounds": {"type": "number"}},
            "required": ["phase"]},
        execute=execute, concurrency_safe=False)
