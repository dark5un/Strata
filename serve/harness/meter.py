"""serve/harness/meter.py — token meter anchored on real usage (ADR-0004).

One estimate per session, folded from the log: the last request/header +
assistant usage gives the anchor (prompt_tokens of the last request); the
surface delta since then is priced with a cheap char/4 estimate. Pressure
checks never re-tokenize. Strata's ByteTokenizer exists but is engine-side;
the harness stays a client of the endpoints (ADR-0002), so estimates are
explicitly estimates and the context limit comes from the monitor.
"""
from __future__ import annotations

from . import events as ev

CHARS_PER_TOKEN = 4.0  # deliberate, documented estimate; good enough for pressure checks


def estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / CHARS_PER_TOKEN))


def fold_meter(rows: list[dict]) -> dict:
    """{anchor_prompt, anchor_completion, anchor_seq, surface_tokens_after} -> current estimate."""
    anchor = None
    for row in rows:
        if row["type"] == ev.ASSISTANT_MESSAGE and row["data"].get("usage"):
            u = row["data"]["usage"]
            anchor = {"prompt": int(u.get("prompt_tokens", 0)),
                      "completion": int(u.get("completion_tokens", 0)), "seq": row["seq"]}
    if anchor is None:
        # no completed request yet: price the whole surface
        total = sum(estimate_tokens(str(r["data"].get("content", ""))) for r in rows
                    if r["type"] in ev.MESSAGE_TYPES)
        return {"prompt_estimate": total, "completion_estimate": 0, "anchor": None}
    after = 0
    for row in rows:
        if row["seq"] <= anchor["seq"]:
            continue
        if row["type"] == ev.USER_MESSAGE:
            after += estimate_tokens(str(row["data"].get("content", "")))
        elif row["type"] == ev.TOOL_RESULT:
            after += estimate_tokens(str(row["data"].get("text", "")))
    return {"prompt_estimate": anchor["prompt"] + anchor["completion"] + after,
            "completion_estimate": anchor["completion"], "anchor": anchor}


def context_pressure(metrics: dict | None, prompt_estimate: int) -> float:
    """0..1 fill of the live context window, from the monitor JSON + our estimate.

    metrics is Service.metrics() (ADR-0002): engine.max_context. Returns 1.0
    when the estimate exceeds the window (overflow: compaction must run).
    """
    max_ctx = ((metrics or {}).get("engine") or {}).get("max_context") or 0
    if max_ctx <= 0:
        return 0.0
    return min(1.0, prompt_estimate / float(max_ctx))
