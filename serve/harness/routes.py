"""serve/harness/routes.py — the /harness/* HTTP surface (ADR-0001, ADR-0003).

server.py mounts handle_get / handle_post at the tail of its route table, so
the fork's cut point is two branches, not a rewrite. Everything here goes
through the HarnessManager on svc.harness (set by --harness); the routes never
call the engine or the loop directly.

Gates (same as upstream's control routes): every /harness/* needs the API key
when one is set (_authorized), and every POST additionally needs _own_page —
JSON from Strata's own page only, so no foreign site can drive the agent.
The SSE stream is a GET: it replays the session log, then pushes new rows.
Approval asks and pending questions ride the same stream as synthetic rows
(seq -1): the durable log records approval/asked + decided when the human
answers, so a reconnecting client can reconcile with GET /harness/events.
"""
from __future__ import annotations

import json
import queue
import time
from urllib.parse import parse_qs, urlsplit

KEEPALIVE_S = 15.0
STREAM_POLL_S = 0.5


def _bad(handler, code: int, message: str) -> None:
    handler._json(code, {"error": {"message": message}})


def _manager(handler, svc):
    mgr = getattr(svc, "harness", None)
    if mgr is None:
        _bad(handler, 404, "the harness is not enabled (start the server with --harness)")
    return mgr


# --------------------------------------------------------------------- GET

def handle_get(handler, svc, path: str) -> None:
    """Path starts with /harness. Handles sessions/events/stream; replies itself."""
    mgr = _manager(handler, svc)
    if mgr is None:
        return
    if not handler._authorized():
        return
    rest = path[len("/harness"):].strip("/")
    query = parse_qs(urlsplit(handler.path).query)
    if rest == "sessions":
        handler._json(200, {"sessions": mgr.list_sessions()})
        return
    if rest == "events":
        sid = (query.get("session") or [""])[0]
        try:
            since = int((query.get("since") or ["0"])[0])
        except ValueError:
            _bad(handler, 400, "since: a whole number")
            return
        try:
            live = mgr.live(sid)
        except FileNotFoundError:
            _bad(handler, 404, f"no session {sid!r}")
            return
        rows = live.handle.snapshot(since)
        # pending asks/questions are process-local (the durable asked/decided rows
        # only land once answered): a reconnecting client needs them to redraw cards
        pending = [{"type": "approval/ask", "seq": -1, "time": None, "data": a}
                   for a in live.channel.pending()]
        pending += [{"type": "question/ask", "seq": -1, "time": None,
                     "data": {"qid": q.qid, "question": q.question, "options": q.options, "multi": q.multi}}
                    for q in live.board.pending()]
        handler._json(200, {"events": rows, "pending": pending, "last_seq": live.handle.last_seq, "busy": live.busy})
        return
    if rest == "stream":
        _stream(handler, mgr, (query.get("session") or [""])[0])
        return
    _bad(handler, 404, "not found")


def _sse_row(handler, row: dict) -> None:
    handler.wfile.write(b"data: " + json.dumps(row, ensure_ascii=False).encode("utf-8") + b"\n\n")


def _stream(handler, mgr, sid: str) -> None:
    if not sid:
        _bad(handler, 400, "stream: ?session=ID")
        return
    try:
        live = mgr.live(sid)
    except FileNotFoundError:
        _bad(handler, 404, f"no session {sid!r}")
        return
    handler._sse()
    sub: queue.Queue = mgr.subscribe(sid)          # subscribe replays the log first
    seen_questions: set[str] = set()
    # reconnect: unanswered asks are still live — replay them and remember them
    seen_asks = {ask["call_id"] for ask in live.channel.pending()}
    for ask in live.channel.pending():
        _sse_row(handler, {"type": "approval/ask", "seq": -1, "time": time.time(), "data": ask})
    last_write = time.monotonic()
    try:
        while True:
            wrote = False
            try:
                _sse_row(handler, sub.get(timeout=STREAM_POLL_S))
                wrote = True
            except queue.Empty:
                pass
            # process-local notices: approval asks (the durable asked/decided rows
            # only land once answered, so the UI needs the live ask)
            try:
                while True:
                    ask = live.approvals.get_nowait()
                    if ask["call_id"] not in seen_asks:
                        seen_asks.add(ask["call_id"])
                        _sse_row(handler, {"type": "approval/ask", "seq": -1, "time": time.time(), "data": ask})
                        wrote = True
            except queue.Empty:
                pass
            for question in live.board.pending():
                if question.qid in seen_questions:
                    continue
                seen_questions.add(question.qid)
                _sse_row(handler, {"type": "question/ask", "seq": -1, "time": time.time(),
                                   "data": {"qid": question.qid, "question": question.question,
                                            "options": question.options, "multi": question.multi}})
                wrote = True
            if not wrote and time.monotonic() - last_write > KEEPALIVE_S:
                handler.wfile.write(b": keep-alive\n\n")   # an SSE comment: clients ignore it
                wrote = True
            if wrote:
                handler.wfile.flush()
                last_write = time.monotonic()
    except (OSError, ValueError):
        pass                                          # the client hung up: end the stream
    finally:
        mgr.unsubscribe(sid, sub)


# -------------------------------------------------------------------- POST

def handle_post(handler, svc, path: str, req: dict) -> None:
    """Path starts with /harness. The body is already parsed JSON (a dict)."""
    mgr = _manager(handler, svc)
    if mgr is None:
        return
    if not handler._own_page("the harness's agent can run tools and change sessions"):
        return
    rest = path[len("/harness"):].strip("/")
    parts = rest.split("/")
    try:
        if rest == "session":
            handler._json(200, {"id": mgr.new()})
            return
        if len(parts) == 3 and parts[0] == "session":
            sid, action = parts[1], parts[2]
            if action == "message":
                content = req.get("content")
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("message: {\"content\": \"...\"}")
                mgr.send(sid, content)
                handler._json(200, {"ok": True, "queued": bool(mgr.live(sid).busy)})
            elif action == "fork":
                boundary = req.get("boundary_seq")
                if boundary is not None and (isinstance(boundary, bool) or not isinstance(boundary, int)):
                    raise ValueError("fork: boundary_seq must be an integer or null")
                handler._json(200, {"id": mgr.fork(sid, boundary)})
            elif action == "cancel":
                handler._json(200, {"cancelled": mgr.cancel(sid)})
            elif action == "answer":
                qid = req.get("qid")
                answer = req.get("answer")
                if not isinstance(qid, str) or not isinstance(answer, dict):
                    raise ValueError("answer: {\"qid\": ..., \"answer\": {...}}")
                handler._json(200, {"ok": mgr.answer(sid, qid, answer)})
            elif action == "decide":
                call_id, outcome = req.get("call_id"), req.get("outcome")
                if not isinstance(call_id, str) or not isinstance(outcome, str):
                    raise ValueError("decide: {\"call_id\": ..., \"outcome\": \"allow-once|deny|cancelled\"}")
                if not mgr.decide(sid, call_id, outcome):
                    _bad(handler, 409, "that approval is no longer pending (already decided, or the turn ended)")
                else:
                    handler._json(200, {"ok": True})
            elif action == "policy":
                mode = req.get("mode")
                allowlist = req.get("allowlist") or []
                if mode not in ("ask", "never", "allowlist") or not isinstance(allowlist, list):
                    raise ValueError("policy: mode ask|never|allowlist, allowlist a list of patterns")
                mgr.set_policy(sid, str(mode), [str(p) for p in allowlist])
                handler._json(200, {"ok": True})
            else:
                _bad(handler, 404, "not found")
            return
        _bad(handler, 404, "not found")
    except FileNotFoundError:
        _bad(handler, 404, f"no session {parts[1] if len(parts) > 1 else ''!r}")
    except ValueError as e:
        _bad(handler, 400, str(e))
