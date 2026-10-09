"""serve/harness/engine.py — the model-call adapter over the in-process Service (ADR-0002).

The harness never speaks HTTP to itself and never touches the engine: it calls
Service.prepare() + Service.run() exactly like the /v1/chat/completions route
does (server.py _openai), and folds the event stream into one EngineResult.

Service.run yields (kind, payload):
  ("ping", None)                    prompt-reading heartbeat
  ("mcp", {...})                    serve/mcp.py progress (not used here: the harness owns its tools)
  ("event", Event)                  frontend OutputParser events: reasoning | content |
                                    tool_start | tool_args | tool_call
  ("done", {...})                   finish + usage: completion_tokens, reused, timings
                                    (prompt_tokens NOT included — the /v1 route
                                    derives it from the prepared ids; we do the same)

The engine is a plain callable for the loop: engine(messages, tools, on_event) -> EngineResult.
on_event receives harness-level stream events (process-local, never logged):
  {"kind": "reasoning"|"content", "text": str}, {"kind": "tool_call", "call": {...}}
  and {"kind": "context", "estimate": int, "pressure": float} (the meter's
  fold at each step boundary — the context pill's single source of truth).
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class EngineResult:
    content: str = ""
    reasoning: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    finish: str = "stop"


@dataclass
class EngineError(Exception):
    message: str
    code: str = "engine_error"

    def __str__(self) -> str:
        return self.message


NOOP = lambda *_a, **_k: None  # noqa: E731


def make_service_engine(svc, sampling: dict | None = None, cancel_poll: Callable[[], bool] | None = None):
    """Bind an engine callable to a live strata serve.server.Service.

    `sampling` rides Service.run's sampling dict (temperature/top_p/...);
    `cancel_poll` is checked on every yielded event so a cancelled turn stops
    the engine like a disconnected client would.
    """
    sampling = dict(sampling or {})

    def engine(messages: list[dict], tools: list[dict] | None, on_event: Callable[[dict], None] = NOOP) -> EngineResult:
        cancel = threading.Event()
        ids, thinking, max_new = svc.prepare(messages, tools, dict(sampling))
        result = EngineResult()
        pending: dict[str, dict] = {}          # call id -> {"id","name","arguments":[str]}
        for kind, x in svc.run(ids, thinking, tools, max_new, sampling, cancel):
            if cancel_poll is not None and cancel_poll():
                cancel.set()
            if kind == "ping":
                continue
            if kind == "mcp":
                continue
            if kind == "event":
                if x.kind == "reasoning" and x.text:
                    result.reasoning += x.text
                    on_event({"kind": "reasoning", "text": x.text})
                elif x.kind == "content" and x.text:
                    result.content += x.text
                    on_event({"kind": "content", "text": x.text})
                elif x.kind == "tool_start":
                    pending[x.call.id] = {"id": x.call.id, "name": x.call.name, "args": []}
                elif x.kind == "tool_args":
                    slot = pending.get(x.call.id)
                    if slot is not None and x.text:
                        slot["args"].append(x.text)
                elif x.kind == "tool_call":
                    call = {"id": x.call.id, "name": x.call.name, "arguments": x.call.arguments}
                    pending.pop(x.call.id, None)
                    result.tool_calls.append(call)
                    on_event({"kind": "tool_call", "call": call})
            elif kind == "done":
                # the real Service.run contract (server.py:3762): ("done", {...})
                # — NOT "final", and the payload carries no prompt_tokens: the
                # /v1 route derives it from the prepared ids (server.py:3996).
                # The ctx-pill 0% bug (2026-10-09): matching "final" left every
                # usage row empty and the meter silently on char/4 estimates.
                result.finish = x.get("finish", "stop")
                result.usage = {
                    "prompt_tokens": x.get("prompt_tokens", len(ids)),
                    "completion_tokens": x.get("completion_tokens", 0),
                    "cached_tokens": x.get("reused") or 0,
                    "timings": x.get("timings") or {},
                }
        # a streamed call whose final tool_call never came (output ended inside): keep the raw args as JSON text
        for slot in pending.values():
            raw = "".join(slot["args"])
            result.tool_calls.append({"id": slot["id"], "name": slot["name"], "arguments_raw": raw})
        if result.finish in ("error",):
            raise EngineError("the engine reported an error for this request")
        return result

    return engine


def make_http_engine(base_url: str, model: str, api_key: str | None = None,
                     sampling: dict | None = None, timeout_s: float = 600.0):
    """Fallback engine: OpenAI chat-completions over HTTP (stdlib urllib), streaming SSE.

    Used by tests against a live server and by setups where the harness runs
    beside (not inside) the model server. Same callable contract as
    make_service_engine.
    """
    import json
    import urllib.request

    sampling = dict(sampling or {})

    def engine(messages: list[dict], tools: list[dict] | None, on_event: Callable[[dict], None] = NOOP) -> EngineResult:
        body: dict = {"model": model, "messages": messages, "stream": True, **sampling}
        if tools:
            body["tools"] = [{"type": "function", "function": t} for t in tools]
        req = urllib.request.Request(
            base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {api_key}"} if api_key else {})})
        result = EngineResult()
        acc: dict[int, dict] = {}
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            for line in resp:
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                payload = line[5:].strip()
                if payload == b"[DONE]":
                    break
                chunk = json.loads(payload)
                if chunk.get("usage"):
                    u = chunk["usage"]
                    result.usage = {"prompt_tokens": u.get("prompt_tokens", 0),
                                    "completion_tokens": u.get("completion_tokens", 0),
                                    "cached_tokens": (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)}
                for choice in chunk.get("choices", []):
                    if choice.get("finish_reason"):
                        result.finish = choice["finish_reason"]
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        result.content += delta["content"]
                        on_event({"kind": "content", "text": delta["content"]})
                    if delta.get("reasoning_content"):
                        result.reasoning += delta["reasoning_content"]
                        on_event({"kind": "reasoning", "text": delta["reasoning_content"]})
                    for tc in delta.get("tool_calls") or []:
                        slot = acc.setdefault(tc.get("index", 0), {"id": None, "name": "", "args": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] += fn["name"]
                        if fn.get("arguments"):
                            slot["args"] += fn["arguments"]
        for idx in sorted(acc):
            slot = acc[idx]
            try:
                args = json.loads(slot["args"]) if slot["args"] else {}
            except json.JSONDecodeError:
                args = {"arguments_raw": slot["args"]}
            call = {"id": slot["id"] or f"call_http_{idx}", "name": slot["name"], "arguments": args}
            result.tool_calls.append(call)
            on_event({"kind": "tool_call", "call": call})
        return result

    return engine
