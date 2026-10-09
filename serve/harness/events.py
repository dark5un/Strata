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

# request reconstruction: full envelope snapshot per model request (M7b: the
# derived messages + sampling + a tool-schema hash — every request is
# reconstructable from log+code). The log is already treated as sensitive.
REQUEST_HEADER = "request/header"

# failed/retried model calls (M7b): invisible today, a retried turn must not
# look identical to a clean one
ASSISTANT_ATTEMPT = "assistant/attempt"

# approvals (fail-closed vocabulary, ADR-0003)
APPROVAL_ASKED = "approval/asked"
APPROVAL_DECIDED = "approval/decided"

# compaction brackets (durable lock: an orphaned bracket means a crash)
COMPACTION_START = "compaction/start"
COMPACTION_SUMMARY = "compaction/summary"
COMPACTION_END = "compaction/end"

# fork boundary marker
SESSION_END_SEED = "session/end-seed"

# modes (log-only state, ADR-0007): plan/mode {active}, goal/change {objective, phase, maxRounds}
PLAN_MODE = "plan/mode"
GOAL_CHANGE = "goal/change"

# meter-driven compaction notice (ADR-0006): why the loop compacted right now
CONTEXT_PRESSURE = "context/pressure"

# subagents (ADR-0008): durable spawn record in the PARENT log — the child
# budget folds from these, so it survives restarts. Settlement rides the
# user/message source vocabulary ("subagent"), not a new message type.
SUBAGENT_SPAWN = "subagent/spawn"

# preset load failed closed (ADR-0009): unknown/malformed preset -> default + note
PRESET_WARNING = "preset/warning"

# session label (M7b/D10): a short model summary of the first ask, appended
# once after the first completed turn; the tab's list prefers it over the
# truncated first message
SESSION_TITLE = "session/title"

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
    PLAN_MODE, GOAL_CHANGE, CONTEXT_PRESSURE, SUBAGENT_SPAWN, PRESET_WARNING,
    ASSISTANT_ATTEMPT, SESSION_TITLE,
})
