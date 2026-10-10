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
    if rest == "ls":
        # M9 (ADR-0015): the workspace picker's directory tree. Directories
        # ONLY: names + a has-more flag, never file contents, never file
        # names. The API key gates it like every other harness route; the
        # listing is bounded (cap + hidden/system dirs skipped) so it cannot
        # be used to slurp a whole filesystem in one call.
        from pathlib import Path as _P
        raw = (query.get("path") or [""])[0]
        base = _P(raw).expanduser() if raw else _P("/")
        try:
            base_r = base.resolve()
        except OSError:
            _bad(handler, 400, "ls: unreadable path")
            return
        if not base_r.is_dir():
            # a file (or anything not a directory) discloses nothing: the
            # route's contract is directories-only (security sweep 2026-10-09)
            _bad(handler, 400, "ls: not a directory")
            return
        cap = 500
        entries = []
        try:
            it = base_r.iterdir()
            while True:
                try:
                    e = next(it)
                except StopIteration:
                    break
                except OSError:
                    continue
                name = e.name
                if name.startswith(".") or name in {"proc", "sys", "dev", "run", "tmp"}:
                    continue
                try:
                    if not e.is_dir():
                        continue
                except OSError:
                    continue
                entries.append({"name": name, "path": str(e)})
                if len(entries) >= cap:
                    break
        except OSError as exc:
            _bad(handler, 403, f"ls: {exc.strerror or 'not readable'}")
            return
        handler._json(200, {"path": str(base_r), "dirs": entries, "truncated": len(entries) >= cap})
        return
    if rest == "presets":
        from .presets import list_presets
        handler._json(200, {"presets": list_presets(mgr.presets_dir)})
        return
    if rest == "preset":
        # read one preset's full schema for the editor (ADR-0011)
        from .presets import find_preset_file, load_preset
        name = (query.get("name") or [""])[0]
        pfile, source = find_preset_file(name, mgr.presets_dir)
        if pfile is None:
            _bad(handler, 404, f"no preset {name!r}")
            return
        preset, _warning = load_preset(name, mgr.presets_dir)
        handler._json(200, {"name": preset.name, "persona": preset.persona, "description": preset.description,
                            "tools": {"allow": preset.allow, "deny": preset.deny}, "source": source})
        return
    if rest == "tools":
        # the live registry's tool names: the preset editor's checkboxes (ADR-0011)
        from .tools import default_registry
        names = {t.name for t in default_registry(root=mgr.workspace_root)}
        if mgr.mcp_hub is not None:
            names |= {t["name"] for t in mgr.mcp_hub.template_tools()}  # ADR-0012
        if mgr.plugins_dir is not None:
            # reviewed plugin tools (ADR-0014): active only — a quarantined
            # plugin has no tools, and the editor must not offer what cannot run
            for p in mgr.active_plugin_tools():
                names.add(p)
        handler._json(200, {"tools": sorted(names)})
        return
    if rest == "plugins":
        # ADR-0014: server-scoped state for the Plugins panel. Off -> 404.
        if mgr.plugins_dir is None:
            _bad(handler, 404, "plugins are off (set STRATA_HARNESS_PLUGINS=1 at server start)")
            return
        from . import plugins as _plugins
        entries = _plugins.scan(mgr.plugins_dir)
        by_name = {e["name"]: e for e in entries}
        for f in mgr.plugin_failures():           # last attach's import/contract errors
            e = by_name.get(f["name"])
            if e is not None:
                e["state"] = "failed"
                e["error"] = f["error"]
        handler._json(200, {"dir": str(mgr.plugins_dir), "plugins": entries})
        return
    if rest == "plugins/source":
        if mgr.plugins_dir is None:
            _bad(handler, 404, "plugins are off (set STRATA_HARNESS_PLUGINS=1 at server start)")
            return
        from . import plugins as _plugins
        view = _plugins.source_view(mgr.plugins_dir, (query.get("name") or [""])[0])
        if view is None:
            _bad(handler, 404, "no such plugin (or a bad name)")
            return
        handler._json(200, view)
        return
    if rest == "export":
        sid = (query.get("session") or [""])[0]
        fmt = (query.get("format") or ["markdown"])[0]
        if fmt not in ("markdown", "jsonl"):
            _bad(handler, 400, "export: format must be markdown|jsonl")
            return
        try:
            live = mgr.live(sid)
        except FileNotFoundError:
            _bad(handler, 404, f"no session {sid!r}")
            return
        from . import export as _export
        if fmt == "jsonl":
            body, ctype, name = _export.to_jsonl(live.handle), "application/x-ndjson", f"{sid}.jsonl"
        else:
            body, ctype, name = _export.to_markdown(live.handle), "text/markdown", f"{sid}.md"
        data = body.encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", f"{ctype}; charset=utf-8")
        handler.send_header("Content-Disposition", f'attachment; filename="{name}"')
        handler._cors()
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)
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
        # the live approval policy (M7d): the tab restores the select + yolo banner
        # on boot/reconnect instead of guessing from browser state
        policy = getattr(live.loop, "_broker", None)
        policy = getattr(policy, "policy", None) if policy is not None else None
        policy_json = ({"mode": policy.mode, "allowlist": policy.allowlist}
                       if policy is not None else {"mode": "ask", "allowlist": []})
        # pending asks/questions are process-local (the durable asked/decided rows
        # only land once answered): a reconnecting client needs them to redraw cards
        pending = [{"type": "approval/ask", "seq": -1, "time": None, "data": a}
                   for a in live.channel.pending()]
        # relayed child asks (ADR-0008): the parent redraws them on reconnect too
        pending += [{"type": "approval/ask", "seq": -1, "time": None, "data": a}
                    for a in mgr.pending_child_asks(sid)]
        pending += [{"type": "question/ask", "seq": -1, "time": None,
                     "data": {"qid": q.qid, "question": q.question, "options": q.options, "multi": q.multi}}
                    for q in live.board.pending()]
        # the pill's source of truth (meter fold, same number compaction uses)
        from .meter import context_pressure, fold_meter
        est = fold_meter(rows)["prompt_estimate"]
        ctx = {"estimate": est, "pressure": round(context_pressure(mgr.metrics_fn(), est), 3)} \
            if mgr.metrics_fn is not None else None
        handler._json(200, {"events": rows, "pending": pending, "last_seq": live.handle.last_seq,
                            "busy": live.busy, "policy": policy_json, "context": ctx})
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
    # (own asks plus relayed child asks, ADR-0008)
    all_asks = list(live.channel.pending()) + mgr.pending_child_asks(sid)
    seen_asks = {ask["call_id"] for ask in all_asks}
    for ask in all_asks:
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
            preset = req.get("preset", "default")
            if not isinstance(preset, str) or not preset.strip():
                raise ValueError("session: preset must be a name string")
            roots = req.get("roots")
            if roots is not None and not isinstance(roots, list):
                raise ValueError("session: roots must be a list of folder paths")
            handler._json(200, {"id": mgr.new(preset=preset.strip(), roots=roots)})
            return
        if rest == "presets":
            # create/update in the USER dir (ADR-0011); the YAML file is the truth
            from .presets import save_preset
            name = req.get("name")
            if not isinstance(name, str):
                raise ValueError("presets: {\"name\": ..., \"persona\": ..., \"tools\": {...}}")
            tools = req.get("tools") or {}
            if not isinstance(tools, dict):
                raise ValueError("presets: tools must be an object")
            allow = tools.get("allow") or []
            deny = tools.get("deny") or []
            if not isinstance(allow, list) or not isinstance(deny, list):
                raise ValueError("presets: tools.allow/deny must be lists")
            persona = req.get("persona")
            description = req.get("description")
            if persona is not None and not isinstance(persona, str):
                raise ValueError("presets: persona must be a string")
            if description is not None and not isinstance(description, str):
                raise ValueError("presets: description must be a string")
            pfile = save_preset(mgr.presets_dir, name.strip(), persona=persona,
                                allow=[str(t) for t in allow], deny=[str(t) for t in deny],
                                description=description)
            handler._json(200, {"ok": True, "name": name.strip(), "path": str(pfile)})
            return
        if rest == "presets/delete":
            # the server has no DELETE verb; the route is POST (ADR-0011: user dir only)
            from .presets import delete_preset
            name = req.get("name")
            if not isinstance(name, str):
                raise ValueError("presets/delete: {\"name\": ...}")
            handler._json(200, {"deleted": delete_preset(mgr.presets_dir, name.strip())})
            return
        if rest in ("plugins/review", "plugins/disable"):
            # ADR-0014: the load gate. review approves the EXACT bytes shown
            # (sha256 must match the current file — TOCTOU guard); disable
            # drops it from the manifest (quarantine at the next attach).
            if mgr.plugins_dir is None:
                _bad(handler, 404, "plugins are off (set STRATA_HARNESS_PLUGINS=1 at server start)")
                return
            from . import plugins as _plugins
            name = req.get("name")
            if not isinstance(name, str):
                raise ValueError(f"{rest}: {{\"name\": ...}}")
            if rest == "plugins/review":
                sha = req.get("sha256")
                if not isinstance(sha, str):
                    raise ValueError("plugins/review: {\"name\": ..., \"sha256\": ...} from the panel")
                result = _plugins.review(mgr.plugins_dir, name.strip(), sha)
                if not result.get("ok"):
                    _bad(handler, 409, result.get("why", "review refused"))
                    return
                mgr.refresh_plugins()   # the tools route reflects the approval now
                handler._json(200, {"ok": True, "note": "approved — loads in NEW sessions (fork to apply to this one)"})
            else:
                ok = _plugins.disable(mgr.plugins_dir, name.strip())
                mgr.refresh_plugins()
                handler._json(200, {"disabled": ok})
            return
        if len(parts) == 3 and parts[0] == "session":
            sid, action = parts[1], parts[2]
            if action == "message":
                content = req.get("content")
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("message: {\"content\": \"...\"}")
                target = req.get("target", "auto")
                if target not in ("auto", "steer"):
                    raise ValueError("message: target must be auto|steer")
                mgr.send(sid, content, target=target)
                handler._json(200, {"ok": True, "queued": bool(mgr.live(sid).busy)})
            elif action == "fork":
                boundary = req.get("boundary_seq")
                if boundary is not None and (isinstance(boundary, bool) or not isinstance(boundary, int)):
                    raise ValueError("fork: boundary_seq must be an integer or null")
                roots = req.get("roots")
                if roots is not None and not isinstance(roots, list):
                    raise ValueError("fork: roots must be a list of folder paths")
                handler._json(200, {"id": mgr.fork(sid, boundary, roots=roots)})
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
                if mode not in ("ask", "never", "allowlist", "yolo") or not isinstance(allowlist, list):
                    raise ValueError("policy: mode ask|never|allowlist|yolo, allowlist a list of patterns")
                mgr.set_policy(sid, str(mode), [str(p) for p in allowlist])
                handler._json(200, {"ok": True})
            elif action == "mode":
                plan = req.get("plan")
                goal = req.get("goal")
                if plan is None and goal is None:
                    raise ValueError("mode: {\"plan\": true|false} and/or {\"goal\": {objective, phase, maxRounds}}")
                if plan is not None and not isinstance(plan, bool):
                    raise ValueError("mode: plan must be a boolean")
                if goal is not None and not isinstance(goal, dict):
                    raise ValueError("mode: goal must be an object")
                mgr.set_mode(sid, plan=plan, goal=goal)
                handler._json(200, {"ok": True})
            else:
                _bad(handler, 404, "not found")
            return
        _bad(handler, 404, "not found")
    except FileNotFoundError:
        _bad(handler, 404, f"no session {parts[1] if len(parts) > 1 else ''!r}")
    except ValueError as e:
        _bad(handler, 400, str(e))
