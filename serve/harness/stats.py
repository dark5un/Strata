"""serve/harness/stats.py — the observability fold (ADR-0017, M11a).

A pure fold over a session's event rows: per-turn aggregates + session
totals. Read + render only — nothing here captures new events; every
number comes from rows the loop already writes (ADR-0004). Unknown
event types are ignored (forward compat).

Context numbers ride the meter fold (ADR-0017 §2): the estimate at
each turn's end seq, priced through context_pressure() with the same
monitor metrics the pill and compaction use — single source of truth.
"""
from __future__ import annotations

from . import events as ev
from .meter import context_pressure, fold_meter

# TOOL_RESULT outcome buckets (ADR-0017 §3)
_SYNTHETIC = frozenset({ev.TOOL_NOT_STARTED, ev.TOOL_ABORTED_BEFORE_DISPATCH,
                        ev.TOOL_OUTCOME_UNKNOWN})


def _tool_bucket(row: dict) -> str:
    data = row["data"]
    if data.get("outcome") in _SYNTHETIC:
        return "unknown"
    return "error" if data.get("is_error") else "ok"


def fold_stats(rows: list[dict], metrics_fn=None) -> dict:
    """Fold event rows into {"turns": [...], "totals": {...}, "children": [...]}.

    A turn is a turn/start..turn/end span; an unclosed span (live
    session) folds with open: true. Approvals fold by call_id:
    approval/asked is a human ask; approval/decided carries
    asked (bool) + outcome (allow-once|deny|cancelled|unavailable|yolo).
    """
    turns: list[dict] = []
    cur: dict | None = None
    # call_id -> tool name, for pairing results/approvals with calls
    names: dict[str, str] = {}
    children: list[dict] = []
    last_usage: dict | None = None
    last_step_ms: int | None = None
    prefix: list[dict] = []          # rows up to the current turn end (meter fold)

    def close_turn(end_row: dict) -> None:
        assert cur is not None
        cur["open"] = False
        cur["endSeq"] = end_row["seq"]
        cur["reason"] = end_row["data"].get("reason", "completed")
        t0 = cur.pop("_t0")
        cur["ms"] = round((end_row["time"] - t0) * 1000) if t0 is not None else None
        _ctx(cur)
        turns.append(cur)

    def _ctx(turn: dict) -> None:
        # meter fold over the turn's prefix: the same number the pill shows
        est = fold_meter(prefix)["prompt_estimate"]
        turn["ctxEstimate"] = est
        turn["ctxPct"] = (round(context_pressure(metrics_fn(), est) * 100, 1)
                          if metrics_fn is not None else None)

    for row in rows:
        t = row["type"]
        if t == ev.TURN_START:
            if cur is not None:            # orphaned bracket (repair appends closers; be tolerant)
                close_turn({"seq": row["seq"] - 1, "time": row["time"], "data": {"reason": ev.REASON_INTERRUPTED}})
            cur = {"n": len(turns) + 1, "startSeq": row["seq"], "endSeq": None,
                   "reason": None, "open": True, "_t0": row["time"], "ms": None,
                   "steps": 0, "tools": {"ok": 0, "error": 0, "unknown": 0, "pending": 0},
                   "toolNames": [], "approvals": {"asked": 0, "allowed": 0, "denied": 0,
                                                  "cancelled": 0, "auto": 0},
                   "compaction": None, "ctxEstimate": None, "ctxPct": None}
        elif t == ev.STEP_END and cur is not None:
            cur["steps"] = max(cur["steps"], int(row["data"].get("step", 0)))
            if isinstance(row["data"].get("ms"), int):
                last_step_ms = row["data"]["ms"]
        elif t == ev.TOOL_CALL and cur is not None:
            cid = row["data"].get("call_id")
            names[cid] = row["data"].get("name", "?")
            cur["toolNames"].append({"name": names[cid], "outcome": "pending", "callId": cid})
            cur["tools"]["pending"] += 1
        elif t == ev.TOOL_RESULT and cur is not None:
            cid = row["data"].get("call_id")
            bucket = _tool_bucket(row)
            for entry in reversed(cur["toolNames"]):
                if entry["callId"] == cid and entry["outcome"] == "pending":
                    entry["outcome"] = bucket
                    if row["data"].get("spill"):
                        entry["spill"] = True
                    break
            else:
                # result without a call in this turn (repair closer): still count it
                cur["toolNames"].append({"name": names.get(cid, "?"), "outcome": bucket, "callId": cid})
            cur["tools"]["pending"] -= 1
            cur["tools"][bucket] += 1
        elif t == ev.APPROVAL_ASKED and cur is not None:
            cur["approvals"]["asked"] += 1
        elif t == ev.APPROVAL_DECIDED and cur is not None:
            outcome = row["data"].get("outcome", "unavailable")
            if not row["data"].get("asked"):
                cur["approvals"]["auto"] += 1
            elif outcome in ("allow-once", "yolo"):
                cur["approvals"]["allowed"] += 1
            elif outcome == "deny":
                cur["approvals"]["denied"] += 1
            else:
                cur["approvals"]["cancelled"] += 1
        elif t == ev.ASSISTANT_MESSAGE and row["data"].get("usage"):
            last_usage = row["data"]["usage"]
        elif t == ev.COMPACTION_END and cur is not None and row["data"].get("committed"):
            cur["compaction"] = {"startSeq": row["data"].get("startSeq"),
                                 "endSeq": row["data"].get("endSeq")}
        elif t == ev.SUBAGENT_SPAWN:
            children.append({"id": row["data"].get("child"), "preset": row["data"].get("preset"),
                             "depth": row["data"].get("depth")})
        elif t == ev.TURN_END and cur is not None:
            close_turn(row)
            cur = None
        prefix.append(row)   # grows with every row; the ctx fold runs at turn end

    if cur is not None:                    # live session: the open turn folds too
        cur.pop("_t0", None)
        cur["ms"] = None
        _ctx(cur)
        turns.append(cur)

    totals = {"turns": len(turns), "steps": 0,
              "tools": {"ok": 0, "error": 0, "unknown": 0, "pending": 0},
              "approvals": {"asked": 0, "allowed": 0, "denied": 0, "cancelled": 0, "auto": 0},
              "tokens": {"prompt": (last_usage or {}).get("prompt_tokens", 0),
                         "completion": (last_usage or {}).get("completion_tokens", 0)},
              "ms": 0, "ctxEstimate": None, "ctxPct": None, "lastStepMs": last_step_ms}
    for turn in turns:
        totals["steps"] += turn["steps"]
        for k in totals["tools"]:
            totals["tools"][k] += turn["tools"][k]
        for k in totals["approvals"]:
            totals["approvals"][k] += turn["approvals"][k]
        if turn["ms"]:
            totals["ms"] += turn["ms"]
    if turns:
        totals["ctxEstimate"] = turns[-1]["ctxEstimate"]
        totals["ctxPct"] = turns[-1]["ctxPct"]

    return {"turns": turns, "totals": totals, "children": children}
