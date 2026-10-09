"""serve/harness/export.py — session export: Markdown (human) / JSONL (raw log).

Markdown is a readable transcript: messages, one-line tool-call summaries,
approval/policy/mode notes, compaction markers. JSONL is the log verbatim —
it contains raw tool output and is sensitive (the UI says so; the security
audit treats session logs as sensitive already).
"""
from __future__ import annotations

import json

from . import events as ev


def _one_line(text: str, limit: int = 160) -> str:
    line = " ".join(str(text).split())
    return line if len(line) <= limit else line[:limit] + "…"


def to_markdown(handle) -> str:
    out: list[str] = [f"# Harness session {handle.header['id']}", ""]
    h = handle.header
    meta = [f"- origin: {h.get('origin', 'user')}", f"- preset: {h.get('agentPreset', 'default')}"]
    if h.get("parentSession"):
        meta.append(f"- parent: {h['parentSession']} (depth {h.get('delegationDepth', 0)})")
    if h.get("cwd"):
        meta.append(f"- cwd: {h['cwd']}")
    out += meta + [""]
    for row in handle.events:
        t, d = row["type"], row.get("data") or {}
        if t == ev.USER_MESSAGE:
            src = d.get("source")
            who = f"**Subagent message**{'' if not src else f' ({src})'}" if src == "subagent" else "**You**"
            out += [f"{who}: {d.get('content', '')}", ""]
        elif t == ev.ASSISTANT_MESSAGE:
            if d.get("reasoning"):
                out += ["<details><summary>Thinking</summary>\n", d["reasoning"], "\n</details>", ""]
            if d.get("content"):
                out += [f"**Agent**: {d['content']}", ""]
            for call in d.get("tool_calls") or []:
                out.append(f"- tool call `{call.get('name')}`: {_one_line(json.dumps(call.get('arguments') or {}))}")
            if d.get("tool_calls"):
                out.append("")
        elif t == ev.TOOL_RESULT:
            flag = " (error)" if d.get("is_error") else ""
            out.append(f"- result `{d.get('call_id')}`{flag}: {_one_line(d.get('text', ''))}")
        elif t == ev.APPROVAL_DECIDED:
            out.append(f"- approval: {d.get('outcome')}")
        elif t == ev.COMPACTION_SUMMARY:
            out.append(f"> earlier context compacted: {_one_line(d.get('content', ''))}")
        elif t == ev.PLAN_MODE:
            out.append(f"- plan mode: {'on' if d.get('active') else 'off'}")
        elif t == ev.GOAL_CHANGE:
            out.append(f"- goal {d.get('phase')}: {_one_line(d.get('objective', ''))}")
        elif t == ev.SUBAGENT_SPAWN:
            out.append(f"- subagent spawned: {d.get('child')} (preset {d.get('preset')}, depth {d.get('depth')})")
        elif t == ev.SESSION_TITLE:
            out.append(f"- session title: {d.get('text')}")
        elif t == ev.TURN_END:
            if d.get("reason") not in (ev.REASON_COMPLETED, None):
                out.append(f"- turn {d.get('reason')}")
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def to_jsonl(handle) -> str:
    header = {"type": "session/header", "seq": -1, "time": None, "data": handle.header}
    lines = [json.dumps(header, ensure_ascii=False)]
    lines += [json.dumps(row, ensure_ascii=False) for row in handle.events]
    return "\n".join(lines) + "\n"
