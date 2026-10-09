"""serve/harness/events.py — the session event vocabulary (ADR-0004).

An event is a dict: {"type": str, "seq": int, "time": float, "data": dict}.
seq is monotonic and contiguous per session; readers MUST ignore event types
they do not know (forward compatibility). The five message-producing types
carry an optional "surfaceOp": either "append" (default) or
{"op": "replace", "startSeq": int, "endSeq": int} which shadows a span of
earlier surface nodes (how compaction rewrites derived history without
touching the log).

This module owns the names and the envelope helpers; session.py owns storage.
"""
from __future__ import annotations

import time

# turn / step brackets
TURN_START = "turn/start"
TURN_END = "turn/end"
STEP_START = "step/start"
STEP_END = "step/end"

# messages (surface-producing)
USER_MESSAGE = "user/message"
ASSISTANT_MESSAGE = "assistant/message"

# tools
TOOL_CALL = "tool/call"
TOOL_RESULT = "tool/result"

# request reconstruction: full GenerateOptions snapshot per model request
REQUEST_HEADER = "request/header"

# approvals (fail-closed vocabulary, ADR-0003)
APPROVAL_ASKED = "approval/asked"
APPROVAL_DECIDED = "approval/decided"

# compaction brackets (durable lock: an orphaned bracket means a crash)
COMPACTION_START = "compaction/start"
COMPACTION_SUMMARY = "compaction/summary"
COMPACTION_END = "compaction/end"

# fork boundary marker
SESSION_END_SEED = "session/end-seed"

# turn/end reasons (durable taxonomy; crash repair appends closers, never truncates)
REASON_COMPLETED = "completed"
REASON_ABORTED = "aborted"
REASON_ERROR = "error"
REASON_BLOCKED = "blocked"
REASON_MAX_TOKENS = "max-tokens"
REASON_INTERRUPTED = "interrupted"
REASON_FORKED = "forked"

# synthetic tool results keep call/result pairing balanced at every prefix
TOOL_NOT_STARTED = "TOOL_NOT_STARTED"
TOOL_ABORTED_BEFORE_DISPATCH = "ABORTED_BEFORE_DISPATCH"
TOOL_OUTCOME_UNKNOWN = "TOOL_OUTCOME_UNKNOWN"

MESSAGE_TYPES = (USER_MESSAGE, ASSISTANT_MESSAGE)


def envelope(event_type: str, data: dict, seq: int, ts: float | None = None) -> dict:
    """Build one event row. Pure: no clock read unless ts is None."""
    return {"type": event_type, "seq": seq, "time": ts if ts is not None else time.time(), "data": data}


def is_known(event_type: str) -> bool:
    return event_type in _KNOWN


_KNOWN = frozenset({
    TURN_START, TURN_END, STEP_START, STEP_END, USER_MESSAGE, ASSISTANT_MESSAGE,
    TOOL_CALL, TOOL_RESULT, REQUEST_HEADER, APPROVAL_ASKED, APPROVAL_DECIDED,
    COMPACTION_START, COMPACTION_SUMMARY, COMPACTION_END, SESSION_END_SEED,
})
