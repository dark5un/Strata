"""serve/harness/modes.py — plan and goal modes: soft guidance, log-only state (ADR-0007).

Both modes are durable session events (plan/mode, goal/change) folded from the
log; nothing about them is stored outside it. Enforcement is deliberately soft:
the modes add a system prompt section and register/unregister affordances,
while the real gates stay the approval policy and the workspace fence (dsh's
plan-mode posture, adopted). The goal round driver lives in the manager: it
queues one goal-sourced user message per idle point while phase is 'active',
and never revives a goal across a restart or fork.
"""
from __future__ import annotations

from . import events as ev

GOAL_PHASES = ("active", "paused", "blocked", "complete")


def fold_plan(rows: list[dict]) -> bool:
    """True when the latest plan/mode event says plan mode is on."""
    active = False
    for row in rows:
        if row["type"] == ev.PLAN_MODE:
            active = bool(row["data"].get("active"))
    return active


def fold_goal(rows: list[dict]) -> dict | None:
    """The latest goal snapshot: {objective, phase, maxRounds} or None."""
    goal: dict | None = None
    for row in rows:
        if row["type"] == ev.GOAL_CHANGE:
            d = row["data"]
            phase = d.get("phase", "active")
            goal = {"objective": str(d.get("objective", "")),
                    "phase": phase if phase in GOAL_PHASES else "active",
                    "maxRounds": int(d.get("maxRounds", 8))}
    return goal


def goal_rounds_used(rows: list[dict]) -> int:
    """Rounds already entered: goal-sourced user messages in the log."""
    return sum(1 for r in rows if r["type"] == ev.USER_MESSAGE and r["data"].get("source") == "goal")


def plan_section(active: bool) -> str | None:
    if not active:
        return None
    return ("PLAN MODE is on: explore and design, do not change the machine. Prefer read/search tools; "
            "avoid write_file, edit_file and shell. When the plan is ready, present it with the "
            "exit_plan_mode tool — the user approves it and normal mode resumes.")


def goal_section(goal: dict | None) -> str | None:
    if not goal or goal["phase"] != "active":
        return None
    return (f"GOAL MODE is on. Objective: {goal['objective']}. Work toward it in focused turns; "
            "when it is fully achieved, call the goal tool with phase=complete; if you are stuck "
            "needing the human, phase=blocked; if you need a decision first, phase=paused.")
