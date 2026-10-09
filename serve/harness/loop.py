"""serve/harness/loop.py — the agent loop (ADR-0003, ADR-0004).

An event-sourced ReAct driver: every turn appends durable brackets to the
session log; the model request is derived from the surface projection, never
from a separately stored history. The engine is a callable:

    engine(messages, tools, on_event) -> {"content": str|None, "reasoning": str,
                                          "tool_calls": [...],
                                          "usage": {"prompt_tokens": int, "completion_tokens": int}}

In production this wraps the in-process Service (ADR-0002); in tests it is a
scripted fake (MockEngine style, no GPU). Cancellation is cooperative: a
threading.Event checked between steps and handed to tool executors.

Policies (approval, compaction) hook the loop at named points; they are not
branches inside it — the dsh waterfall idea, kept small.

M7 (transcript honesty + observability):
  * assistant/message carries `reasoning` (durable, human-facing; never fed
    back into the derived history — token cost, and the Chat tab's server
    strips reasoning-only turns the same way).
  * on_stream: process-local delta callback (reasoning/content/tool_call) —
    the tab paints a provisional bubble while the model works (never logged).
  * request/header carries the FULL envelope (derived messages + sampling +
    tool-schema hash): every model request is reconstructable from log+code.
  * assistant/attempt records failed model calls; step/end carries wall time
    and the engine's prefill/decode split when it reported one.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import events as ev
from .session import SessionHandle

if TYPE_CHECKING:
    from .inbox import Inbox
    from .spill import SpillStore

MAX_PARALLEL_TOOL_CALLS = 4


def normalize_result(raw):
    """Engines return engine.EngineResult; plain dicts are tolerated (tests, adapters) and converted."""
    from .engine import EngineResult
    if isinstance(raw, EngineResult):
        return raw
    return EngineResult(content=raw.get("content") or "", reasoning=raw.get("reasoning", ""),
                        tool_calls=raw.get("tool_calls") or [], usage=raw.get("usage") or {},
                        finish=raw.get("finish", "stop"))


class Cancelled(Exception):
    pass


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict                      # JSON Schema (model-facing only)
    execute: Callable                     # (args: dict, ctx: ToolRunContext) -> {"text": str, "is_error": bool}
    concurrency_safe: bool = True
    timeout_s: float = 120.0
    needs_approval: bool = True           # gated by the approval policy unless the registry clears it


@dataclass
class ToolRunContext:
    session: SessionHandle
    cancel: threading.Event
    conclude_turn: Callable[[], None] = lambda: None


@dataclass
class ApprovalRequest:
    call_id: str
    tool: str
    args: dict                            # FULL arguments: matching and the card both read these (ADR-0010)
    args_preview: str = ""                # display fallback only, never a matching input


@dataclass
class Loop:
    """One loop drives one session. Single writer: run it on one worker thread."""
    engine: Callable
    tools: dict[str, ToolDefinition]
    session: SessionHandle
    # approval: (ApprovalRequest) -> str outcome, or (outcome, asked) tuple; fail-closed.
    # None means deny-by-default: an unconfigured harness never runs gated tools.
    approval: Callable[[ApprovalRequest], str | tuple[str, bool]] | None = None
    # pre_step: (loop, inbox) -> optional extra user messages to inject before the step.
    pre_step: list[Callable] = field(default_factory=list)
    # system_sections: () -> str | None, joined into one system message at derive time
    # (plan/goal guidance rides these; ADR-0007 keeps modes soft, prompt-only).
    system_sections: list[Callable] = field(default_factory=list)
    max_steps_per_turn: int = 64
    spill: SpillStore | None = None        # oversized tool output -> file + locator
    inbox: Inbox | None = None             # steer/inject between steps
    cancel: threading.Event = field(default_factory=threading.Event)  # cooperative turn cancel
    # meter-driven compaction (ADR-0006): metrics_fn is Service.metrics() (ADR-0002);
    # when context pressure crosses the threshold the loop compacts before the next step.
    metrics_fn: Callable[[], dict] | None = None
    compact_at: float = 0.80
    # sampling rides the request/header envelope (M7b: reconstructable requests)
    sampling: dict = field(default_factory=dict)
    # on_stream: process-local delta sink (M7a-3): {"kind": "reasoning"|"content"|"tool_call", ...}
    # — never logged; the manager fans it out to SSE subscribers as synthetic rows.
    on_stream: Callable[[dict], None] | None = None

    def request_cancel(self) -> None:
        """Cooperative cancellation: checked at step boundaries and handed to tool contexts."""
        self.cancel.set()

    def send(self, content: str) -> dict:
        """Run one full turn for a user message; returns the turn/end data."""
        self.session.append(ev.USER_MESSAGE, {"content": content})
        return self.run_turn()

    def run_turn(self) -> dict:
        self.cancel.clear()                       # a fresh turn: a previous cancel must not poison it
        self.session.append(ev.TURN_START, {})
        self.session.flush()
        steps = 0
        try:
            while steps < self.max_steps_per_turn:
                if self.cancel.is_set():
                    raise Cancelled
                steps += 1
                if self.inbox is not None:
                    for msg in self.inbox.drain("next-step"):
                        if msg.preset != "inject":
                            self.session.append(ev.USER_MESSAGE, {"content": msg.content, "source": msg.preset})
                for hook in self.pre_step:
                    hook(self)
                self._maybe_compact()
                self.session.append(ev.STEP_START, {"step": steps})
                messages = self._derive_with_request_header()
                tool_schemas = [
                    {"name": t.name, "description": t.description, "parameters": t.parameters}
                    for t in self.tools.values()
                ]
                step_t0 = time.monotonic()
                try:
                    result = normalize_result(self._call_engine(messages, tool_schemas))
                except Exception as exc:                       # noqa: BLE001 - settle, never kill the server
                    self.session.append(ev.STEP_END, {"step": steps, "error": str(exc),
                                                      "ms": round((time.monotonic() - step_t0) * 1000)})
                    return self._end_turn(ev.REASON_ERROR, {"message": str(exc)})
                calls = result.tool_calls
                self.session.append(ev.ASSISTANT_MESSAGE, {
                    "content": result.content,
                    "reasoning": result.reasoning or None,     # M7a-1: durable, human-facing (forward-compat field)
                    "tool_calls": calls or None,
                    "usage": result.usage,
                })
                step_data: dict = {"step": steps, "ms": round((time.monotonic() - step_t0) * 1000)}
                timings = (result.usage or {}).get("timings")
                if timings:                                    # M7b-3: prefill/decode split when the engine reported it
                    step_data["prompt_ms"] = timings.get("prompt_ms")
                    step_data["decode_ms"] = timings.get("predicted_ms", timings.get("decode_ms"))
                if not calls:
                    self.session.append(ev.STEP_END, step_data)
                    self.session.flush()
                    return self._end_turn(ev.REASON_COMPLETED, {})
                concluded = self._dispatch(calls)
                self.session.append(ev.STEP_END, step_data)
                self.session.flush()
                if concluded:
                    return self._end_turn(ev.REASON_COMPLETED, {"concluded": True})
            return self._end_turn(ev.REASON_BLOCKED, {"why": "max_steps_per_turn"})
        except Cancelled:
            return self._end_turn(ev.REASON_ABORTED, {"cause": "user"})

    def _call_engine(self, messages: list[dict], tool_schemas: list[dict]):
        """One model call with the stream sink wired (M7a-3). A failed call appends an
        assistant/attempt row (M7b-2) before re-raising: a retried turn must not look
        identical to a clean one."""
        if self.on_stream is None:
            t0 = time.monotonic()
            try:
                return self.engine(messages, tool_schemas)
            except Exception as exc:  # noqa: BLE001
                self.session.append(ev.ASSISTANT_ATTEMPT, {"status": "failed", "error": str(exc),
                                                           "ms": round((time.monotonic() - t0) * 1000)})
                raise
        def on_event(d: dict) -> None:
            if self.on_stream is not None:
                self.on_stream(d)
        t0 = time.monotonic()
        try:
            return self.engine(messages, tool_schemas, on_event)
        except Exception as exc:  # noqa: BLE001
            self.session.append(ev.ASSISTANT_ATTEMPT, {"status": "failed", "error": str(exc),
                                                       "ms": round((time.monotonic() - t0) * 1000)})
            raise

    def _maybe_compact(self) -> None:
        """Meter-driven compaction (ADR-0006): fold the meter, read the monitor's
        max_context, and if pressure crosses compact_at, prune first (free) and
        summarize second (one model call). Never runs mid-pair: it sits at the
        step boundary, where the surface is balanced."""
        if self.metrics_fn is None:
            return
        from .compaction import prune_tool_results, summarize_oldest
        from .meter import context_pressure, fold_meter
        estimate = fold_meter(self.session.snapshot())["prompt_estimate"]
        try:
            pressure = context_pressure(self.metrics_fn(), estimate)
        except Exception:                                   # noqa: BLE001 - monitor read is best-effort
            return
        if pressure < self.compact_at:
            return
        self.session.append(ev.CONTEXT_PRESSURE, {"estimate": estimate, "pressure": round(pressure, 3),
                                                  "action": "compact"})
        pruned = prune_tool_results(self.session)
        after = fold_meter(self.session.snapshot())["prompt_estimate"]
        if context_pressure(self.metrics_fn(), after) >= self.compact_at or pruned == 0:
            summarize_oldest(self.session, self.engine)

    def _derive_with_request_header(self) -> list[dict]:
        from .session import derive_messages
        messages = derive_messages(self.session)
        sections = [s for s in (fn() for fn in self.system_sections) if s]
        if sections:
            messages.insert(0, {"role": "system", "content": "\n\n".join(sections)})
        # M7b-1: the full envelope — every model request is reconstructable from
        # log+code (replay, crash forensics, cache reasoning). The session log is
        # already treated as sensitive (security audit 2016-10-09); no new exposure.
        schemas = [{"name": t.name, "parameters": t.parameters} for t in self.tools.values()]
        self.session.append(ev.REQUEST_HEADER, {
            "model": "strata", "message_count": len(messages),
            "tools": sorted(self.tools),
            "messages": messages,
            "sampling": dict(self.sampling),
            "tool_schemas_sha256": hashlib.sha256(
                json.dumps(schemas, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        })
        return messages

    def _dispatch(self, calls: list[dict]) -> bool:
        """Execute tool calls (bounded pool; unsafe calls are barriers). Returns True if a call concluded the turn."""
        concluded = {"v": False}
        pool: ThreadPoolExecutor | None = None
        for call in calls:
            name = call.get("name") or "?"
            args = call.get("arguments") or {}
            call_id = call.get("id") or f"call_{self.session.last_seq}_{name}"
            self.session.append(ev.TOOL_CALL, {"call_id": call_id, "name": name, "arguments": args})
            tool = self.tools.get(name)
            if tool is None:
                self._result(call_id, {"text": f"unknown tool {name!r}", "is_error": True})
                continue
            if getattr(tool, "needs_approval", True):
                decision = self._ask_approval(call_id, tool, args)
                if decision not in ("allow-once", "yolo"):
                    self._result(call_id, {"text": f"approval {decision}", "is_error": True})
                    continue
            if not tool.concurrency_safe:
                if pool is not None:
                    _drain(pool)
                    pool = None
                if self._run_tool(tool, call_id, args, concluded):
                    concluded["v"] = True
                continue
            if pool is None:
                pool = ThreadPoolExecutor(max_workers=MAX_PARALLEL_TOOL_CALLS)
            pool.submit(self._run_tool, tool, call_id, args, concluded)
        if pool is not None:
            _drain(pool)
        return concluded["v"]

    def _ask_approval(self, call_id: str, tool: ToolDefinition, args: dict) -> str:
        req = ApprovalRequest(call_id=call_id, tool=tool.name, args=args, args_preview=_preview(args))
        if self.approval is None:
            outcome, asked = "unavailable", True          # fail closed: no answerer configured
        else:
            try:
                ret = self.approval(req)
            except Exception:                            # noqa: BLE001 - fail closed
                ret = ("unavailable", True)
            if isinstance(ret, tuple):
                outcome, asked = ret
            else:
                outcome, asked = ret, True               # plain-outcome callbacks always count as an ask
            if outcome not in ("allow-once", "deny", "cancelled", "unavailable", "yolo"):
                outcome = "unavailable"
        if asked:
            self.session.append(ev.APPROVAL_ASKED, {"call_id": call_id, "tool": tool.name,
                                                    "preview": req.args_preview})
        self.session.append(ev.APPROVAL_DECIDED, {"call_id": call_id, "tool": tool.name,
                                                  "outcome": outcome, "asked": asked})
        return outcome

    def _run_tool(self, tool: ToolDefinition, call_id: str, args: dict, concluded: dict) -> bool:
        """Run one tool; returns True if the tool concluded the turn. Thread-safe: results are
        appended under the session's write lock (single writer per append)."""
        done: dict = {}

        def ctx_conclude() -> None:
            concluded["v"] = True

        ctx = ToolRunContext(session=self.session, cancel=self.cancel, conclude_turn=ctx_conclude)

        def target() -> None:
            try:
                done["r"] = tool.execute(args, ctx)
            except Exception as exc:                            # noqa: BLE001
                done["r"] = {"text": f"tool error: {exc}", "is_error": True}

        t = threading.Thread(target=target, daemon=True)
        t.start()
        t.join(timeout=tool.timeout_s)
        if t.is_alive():
            self._result(call_id, {"text": f"tool {tool.name} timed out", "is_error": True,
                                   "outcome": ev.TOOL_OUTCOME_UNKNOWN})
            return False
        r = done.get("r") or {"text": "", "is_error": True}
        self._result(call_id, r)
        return concluded["v"]

    def _result(self, call_id: str, r: dict) -> None:
        payload = {"call_id": call_id, "text": r.get("text", ""), "is_error": bool(r.get("is_error")),
                   "outcome": r.get("outcome")}
        if self.spill is not None:
            from .spill import spill_policy
            spilled = spill_policy(str(r.get("text", "")), self.spill)
            payload["text"] = spilled["text"]
            if "spill" in spilled:
                payload["spill"] = spilled["spill"]
                payload["retrievalHint"] = spilled["retrievalHint"]
        if r.get("meta"):
            payload["meta"] = r["meta"]
        self.session.append(ev.TOOL_RESULT, payload)

    def _end_turn(self, reason: str, data: dict) -> dict:
        payload = {"reason": reason, **data}
        self.session.append(ev.TURN_END, payload)
        self.session.flush()
        return payload


def _drain(pool: ThreadPoolExecutor) -> None:
    pool.shutdown(wait=True)


def _preview(args: dict) -> str:
    """Display fallback only (the durable asked row's one-liner). Matching and
    the approval CARD use the full args (ADR-0010); this never gates anything."""
    text = " ".join(str(v) for v in args.values())
    return text[:200]
