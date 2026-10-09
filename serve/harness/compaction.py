"""serve/harness/compaction.py — surface-shadowing compaction (ADR-0004).

Two strategies, both log-only (the raw log is never rewritten):

  prune_tool_results: deterministic head/middle/tail trim of oversized
    tool/result rows — replaces each with a shadow node (surfaceOp replace)
    citing the original seq. Cheap, no model call, first line of defence.

  summarize: one model call over the oldest span; the summary lands as a
    compaction/summary surface node that shadows [startSeq, endSeq]. The
    compaction/start...end bracket is a durable lock: an orphaned bracket on
    open means a crash mid-compaction (detectable, replayable).

Pairing rule: a span boundary never splits a tool/call from its tool/result —
the region edges are adjusted outward to keep every prefix balanced.
"""
from __future__ import annotations

from . import events as ev
from .session import SessionHandle

PRUNE_KEEP_HEAD = 1200
PRUNE_KEEP_TAIL = 1200
PRUNE_MIN_CHARS = 3000


def tool_pairs_balanced(rows: list[dict]) -> bool:
    """Every tool/call in rows has its tool/result in rows (and vice versa)."""
    calls = {r["data"]["call_id"] for r in rows if r["type"] == ev.TOOL_CALL}
    results = {r["data"]["call_id"] for r in rows if r["type"] == ev.TOOL_RESULT}
    return calls == results


def _pair_safe_start(handle: SessionHandle, start_seq: int) -> int:
    """Move the span start back so it never begins inside a call/result pair."""
    rows = handle.events
    ids_in_span = set()
    for row in rows:
        if row["seq"] >= start_seq and row["type"] in (ev.TOOL_CALL, ev.TOOL_RESULT):
            ids_in_span.add(row["data"]["call_id"])
    changed = True
    while changed:
        changed = False
        for row in rows:
            if row["seq"] >= start_seq:
                continue
            if row["type"] in (ev.TOOL_CALL, ev.TOOL_RESULT) and row["data"]["call_id"] in ids_in_span:
                start_seq = row["seq"]
                ids_in_span.add(row["data"]["call_id"])
                changed = True
    return start_seq


def prune_tool_results(handle: SessionHandle) -> int:
    """Shadow oversized tool results with head/tail + locator. Returns count pruned."""
    pruned = 0
    for row in list(handle.events):
        if row["type"] != ev.TOOL_RESULT:
            continue
        text = str(row["data"].get("text", ""))
        if len(text) <= PRUNE_MIN_CHARS:
            continue
        shadow = {
            "call_id": row["data"]["call_id"],
            "text": text[:PRUNE_KEEP_HEAD] + f"\n[... {len(text) - PRUNE_KEEP_HEAD - PRUNE_KEEP_TAIL} chars pruned ...]\n"
                  + text[-PRUNE_KEEP_TAIL:],
            "is_error": bool(row["data"].get("is_error")),
            "pruned_from_seq": row["seq"],
        }
        handle.append(ev.TOOL_RESULT, shadow,
                      surfaceOp={"op": "replace", "startSeq": row["seq"], "endSeq": row["seq"]},
                      sourceEventSeqs=[row["seq"]])
        pruned += 1
    if pruned:
        handle.flush()
    return pruned


def summarize_oldest(handle: SessionHandle, engine, keep_last_turns: int = 2) -> bool:
    """One model call summarizing everything before the last keep_last_turns turns.

    engine: the loop's engine callable (messages, tools) -> EngineResult.
    Returns True if a summary was committed.
    """
    turn_starts = [r["seq"] for r in handle.events if r["type"] == ev.TURN_START]
    if len(turn_starts) <= keep_last_turns:
        return False
    start_seq = _pair_safe_start(handle, turn_starts[0])
    end_seq = turn_starts[-keep_last_turns] - 1
    span = [r for r in handle.events if start_seq <= r["seq"] <= end_seq]
    if not span or not tool_pairs_balanced(span):
        return False
    transcript = "\n".join(
        f"{r['type']}: {str(r['data'].get('content') or r['data'].get('text') or '')[:400]}"
        for r in span if r["type"] in ev.MESSAGE_TYPES or r["type"] == ev.TOOL_RESULT)
    handle.append(ev.COMPACTION_START, {"startSeq": start_seq, "endSeq": end_seq})
    handle.flush()
    try:
        result = engine([{"role": "user", "content":
                          "Summarize this agent transcript in <=300 words, keeping every decision, "
                          "file touched, command run and open task:\n\n" + transcript}], None)
    except Exception:  # noqa: BLE001 — bracket closes fail-closed
        handle.append(ev.COMPACTION_END, {"committed": False})
        handle.flush()
        return False
    handle.append(ev.COMPACTION_SUMMARY, {"content": f"[summary of earlier work]\n{result.content}"},
                  surfaceOp={"op": "replace", "startSeq": start_seq, "endSeq": end_seq},
                  sourceEventSeqs=[r["seq"] for r in span])
    handle.append(ev.COMPACTION_END, {"committed": True, "startSeq": start_seq, "endSeq": end_seq})
    handle.flush()
    return True


def orphaned_bracket(handle: SessionHandle) -> dict | None:
    """Crash detection: a compaction/start with no matching end. Returns the open bracket."""
    open_b = None
    for row in handle.events:
        if row["type"] == ev.COMPACTION_START:
            open_b = row
        elif row["type"] == ev.COMPACTION_END:
            open_b = None
    return open_b
