"""serve/harness/test_routes.py — the /harness/* HTTP surface end-to-end.

A real serve() with a MockEngine Service and a scripted HarnessManager:
no GPU, no browser. Covers the gates (404 without --harness, 415 non-JSON,
401 with an API key), the command routes, and the SSE stream (replay,
live rows, approval asks; keep-alives are comment frames and ignored).

    python -m unittest serve.harness.test_routes -v
"""
from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from serve.frontend import ChatTemplate
from serve.server import ByteTokenizer, MockEngine, Service, serve

from .engine import EngineResult
from .manager import HarnessManager

ROOT = Path(__file__).resolve().parents[2]


def scripted(script):
    box = {"i": 0}

    def factory():
        def engine(messages, tools, on_event=None):
            i = box["i"]
            box["i"] += 1
            if i >= len(script):
                return EngineResult(content="done", usage={"prompt_tokens": 1, "completion_tokens": 1})
            return script[i]
        return engine
    return factory


class RoutesBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        tok = ByteTokenizer()
        self.svc = Service(MockEngine(tok, ["</think>\n\nmock"]), tok,
                           ChatTemplate(ROOT / "serve/chat_template.jinja"))
        self.mgr = HarnessManager(self.root / "sessions", scripted(
            [EngineResult(content="hi", usage={"prompt_tokens": 2, "completion_tokens": 1})]),
            spill_dir=self.root / "spill", workspace_root=self.root)
        # ADR-0011: CRUD writes to the USER dir — point it at the temp root so the
        # real ~/.local/share is never touched by tests (presets_dir stays None:
        # the bundled listing must still resolve)
        import os
        self._env_key = "STRATA_HARNESS_PRESET_DIR"
        self._saved_env = os.environ.get(self._env_key)
        os.environ[self._env_key] = str(self.root / "user-presets")
        self.svc.harness = self.mgr  # type: ignore[attr-defined]  # the fork's attribute: set by integrate.attach, read via getattr (ADR-0001: no upstream class edits)
        self.httpd = serve(self.svc, port=0)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._tmp.cleanup()
        import os
        if self._saved_env is None:
            os.environ.pop(self._env_key, None)
        else:
            os.environ[self._env_key] = self._saved_env

    def get(self, path):
        req = urllib.request.Request(self.base + path)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def post(self, path, body, content_type="application/json"):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": content_type})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def wait_idle(self, sid: str, timeout: float = 5.0) -> dict:
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, ev = self.get(f"/harness/events?session={sid}")
            if not ev["busy"]:
                return ev
            time.sleep(0.05)
        raise TimeoutError(f"session {sid} still busy")


class GateTests(RoutesBase):
    def test_404_when_harness_off(self):
        self.svc.harness = None  # type: ignore[attr-defined]
        code, body = self.get("/harness/sessions")
        self.assertEqual(code, 404)
        self.assertIn("not enabled", body["error"]["message"])

    def test_post_needs_json_content_type(self):
        code, _ = self.post("/harness/session", {}, content_type="text/plain")
        self.assertEqual(code, 415)

    def test_api_key_gates_the_routes(self):
        key = "har" + "ness-test-key"
        self.svc.api_key = key
        try:
            code, body = self.get("/harness/sessions")
            self.assertEqual(code, 401)
            self.assertEqual(body["error"]["type"], "authentication_error")
            req = urllib.request.Request(self.base + "/harness/sessions",
                                         headers={"x-api-key": key})
            with urllib.request.urlopen(req, timeout=10) as r:
                self.assertEqual(r.status, 200)
        finally:
            self.svc.api_key = ""


class SessionCommandTests(RoutesBase):
    def test_create_message_and_events(self):
        code, created = self.post("/harness/session", {})
        self.assertEqual(code, 200)
        sid = created["id"]
        code, _ = self.post(f"/harness/session/{sid}/message", {"content": "hello"})
        self.assertEqual(code, 200)
        ev = self.wait_idle(sid)
        types = [r["type"] for r in ev["events"]]
        self.assertIn("user/message", types)
        self.assertIn("assistant/message", types)
        self.assertIn("turn/end", types)          # M7: session/title may land after it
        code, listing = self.get("/harness/sessions")
        self.assertEqual([s["id"] for s in listing["sessions"]], [sid])

    def test_message_validation(self):
        _, created = self.post("/harness/session", {})
        code, _ = self.post(f"/harness/session/{created['id']}/message", {"content": "  "})
        self.assertEqual(code, 400)
        code, _ = self.post("/harness/session/nosuch/message", {"content": "x"})
        self.assertEqual(code, 404)

    def test_mode_route_plan_and_goal(self):
        _, created = self.post("/harness/session", {})
        sid = created["id"]
        code, _ = self.post(f"/harness/session/{sid}/mode", {"plan": True})
        self.assertEqual(code, 200)
        code, _ = self.post(f"/harness/session/{sid}/mode",
                            {"goal": {"objective": "ship", "phase": "active", "maxRounds": 3}})
        self.assertEqual(code, 200)
        ev = self.wait_idle(sid)
        types = [r["type"] for r in ev["events"]]
        self.assertIn("plan/mode", types)
        self.assertIn("goal/change", types)
        # validation: empty mode body, bad plan type, bad goal type
        for bad in ({}, {"plan": "yes"}, {"goal": "ship"}):
            code, _ = self.post(f"/harness/session/{sid}/mode", bad)
            self.assertEqual(code, 400)

    def test_message_target_validation(self):
        _, created = self.post("/harness/session", {})
        code, _ = self.post(f"/harness/session/{created['id']}/message",
                            {"content": "x", "target": "nowhere"})
        self.assertEqual(code, 400)

    def test_fork_and_cancel_and_policy(self):
        _, created = self.post("/harness/session", {})
        sid = created["id"]
        self.post(f"/harness/session/{sid}/message", {"content": "one"})
        self.wait_idle(sid)
        code, kid = self.post(f"/harness/session/{sid}/fork", {})
        self.assertEqual(code, 200)
        self.assertNotEqual(kid["id"], sid)
        code, _ = self.post(f"/harness/session/{sid}/cancel", {})
        self.assertEqual(code, 200)
        code, _ = self.post(f"/harness/session/{sid}/policy", {"mode": "never", "allowlist": []})
        self.assertEqual(code, 200)
        code, _ = self.post(f"/harness/session/{sid}/policy", {"mode": "yolo", "allowlist": []})
        self.assertEqual(code, 200)          # ADR-0010: yolo is a valid mode
        code, _ = self.post(f"/harness/session/{sid}/policy", {"mode": "swarm"})
        self.assertEqual(code, 400)

    def test_decide_unknown_call_is_409(self):
        _, created = self.post("/harness/session", {})
        code, _ = self.post(f"/harness/session/{created['id']}/decide",
                            {"call_id": "nope", "outcome": "deny"})
        self.assertEqual(code, 409)

    def test_presets_route(self):
        code, body = self.get("/harness/presets")
        self.assertEqual(code, 200)
        names = [p["name"] for p in body["presets"]]
        self.assertIn("default", names)
        self.assertIn("researcher", names)          # the bundled example
        src = {p["name"]: p["source"] for p in body["presets"]}
        self.assertEqual(src["researcher"], "bundled")

    def test_preset_crud_routes(self):
        # ADR-0011: create -> read -> list (user badge) -> delete, in the manager's temp dir
        code, _ = self.post("/harness/presets", {"name": "mine", "persona": "be careful",
                                                 "tools": {"allow": ["read_file"], "deny": ["shell"]},
                                                 "description": "my preset"})
        self.assertEqual(code, 200)
        code, one = self.get("/harness/preset?name=mine")
        self.assertEqual(code, 200)
        self.assertEqual(one["persona"], "be careful")
        self.assertEqual(one["tools"], {"allow": ["read_file"], "deny": ["shell"]})
        self.assertEqual(one["source"], "user")
        _, listing = self.get("/harness/presets")
        self.assertEqual(next(p for p in listing["presets"] if p["name"] == "mine")["source"], "user")
        code, _ = self.post("/harness/presets/delete", {"name": "mine"})
        self.assertEqual(code, 200)
        code, _ = self.get("/harness/preset?name=mine")
        self.assertEqual(code, 404)

    def test_preset_validation(self):
        code, _ = self.post("/harness/presets", {"name": "not a name!"})
        self.assertEqual(code, 400)
        code, _ = self.post("/harness/presets", {"name": "default"})
        self.assertEqual(code, 400)
        code, _ = self.post("/harness/presets", {"name": "ok", "persona": 5})
        self.assertEqual(code, 400)
        code, _ = self.get("/harness/preset?name=nope")
        self.assertEqual(code, 404)

    def test_tools_route_lists_the_registry(self):
        code, body = self.get("/harness/tools")
        self.assertEqual(code, 200)
        self.assertIn("shell", body["tools"])
        self.assertIn("read_file", body["tools"])

    def test_new_session_preset_validation(self):
        code, _ = self.post("/harness/session", {"preset": 5})
        self.assertEqual(code, 400)
        code, body = self.post("/harness/session", {"preset": "researcher"})
        self.assertEqual(code, 200)
        _, listing = self.get("/harness/sessions")
        row = next(s for s in listing["sessions"] if s["id"] == body["id"])
        self.assertEqual(row["preset"], "researcher")
        self.assertEqual(row["origin"], "user")


    def test_export_routes(self):
        _, created = self.post("/harness/session", {})
        sid = created["id"]
        self.post(f"/harness/session/{sid}/message", {"content": "hello"})
        self.wait_idle(sid)
        import urllib.request
        with urllib.request.urlopen(f"{self.base}/harness/export?session={sid}&format=markdown", timeout=10) as r:
            self.assertEqual(r.status, 200)
            self.assertIn("attachment", r.headers.get("Content-Disposition", ""))
            text = r.read().decode()
        self.assertIn(f"# Harness session {sid}", text)
        self.assertIn("hello", text)
        with urllib.request.urlopen(f"{self.base}/harness/export?session={sid}&format=jsonl", timeout=10) as r:
            lines = r.read().decode().splitlines()
        self.assertEqual(json.loads(lines[0])["type"], "session/header")
        code, _ = self.get(f"/harness/export?session={sid}&format=exe")
        self.assertEqual(code, 400)
        code, _ = self.get("/harness/export?session=nope&format=markdown")
        self.assertEqual(code, 404)

    def test_events_include_pending_asks(self):
        # a blocked approval must be visible through /harness/events (reconnect path)
        self.mgr.engine_factory = scripted([
            EngineResult(tool_calls=[{"id": "c1", "name": "shell", "arguments": {"command": "echo p"}}],
                         usage={"prompt_tokens": 3, "completion_tokens": 2}),
            EngineResult(content="ok", usage={}),
        ])
        _, created = self.post("/harness/session", {})
        sid = created["id"]
        self.post(f"/harness/session/{sid}/message", {"content": "go"})
        live = self.mgr.live(sid)
        live.approvals.get(timeout=3)                 # the ask lands
        _, ev = self.get(f"/harness/events?session={sid}")
        self.assertEqual([p["type"] for p in ev["pending"]], ["approval/ask"])
        self.assertEqual(ev["pending"][0]["data"]["call_id"], "c1")
        self.mgr.decide(sid, "c1", "deny")            # settle the worker for teardown

    def test_events_carry_the_meter_fold(self):
        # ctx-pill 0% bug: /harness/events carries the meter's fold so a
        # reconnecting tab paints a plausible context pill at once
        import time
        self.mgr.metrics_fn = lambda: {"engine": {"max_context": 1000}}
        _, created = self.post("/harness/session", {})
        sid = created["id"]
        self.post(f"/harness/session/{sid}/message", {"content": "a task with words"})
        for _ in range(200):
            if not self.mgr.live(sid).busy:
                break
            time.sleep(0.02)
        _, ev = self.get(f"/harness/events?session={sid}")
        self.assertIsNotNone(ev["context"])
        self.assertGreater(ev["context"]["estimate"], 0)

    def test_session_roots_roundtrip_and_fork_narrowing(self):
        # M9: roots ride the create call, show in the list, and a fork may
        # narrow but never widen them.
        ws = self.root / "ws"
        (ws / "proj").mkdir(parents=True)
        code, created = self.post("/harness/session", {"roots": [str(ws)]})
        self.assertEqual(code, 200)
        sid = created["id"]
        _, listing = self.get("/harness/sessions")
        row = next(s for s in listing["sessions"] if s["id"] == sid)
        self.assertEqual(row["roots"], [str(ws.resolve())])
        code, bad = self.post("/harness/session", {"roots": [str(self.root / "nope")]})
        self.assertEqual(code, 400)                       # not a directory
        self.assertIn("not a directory", bad["error"]["message"])
        code, wide = self.post(f"/harness/session/{sid}/fork",
                               {"roots": [str(self.root / "outside")]})
        self.assertEqual(code, 400)                       # widening refused
        code, ok = self.post(f"/harness/session/{sid}/fork", {"roots": [str(ws / "proj")]})
        self.assertEqual(code, 200)
        _, listing = self.get("/harness/sessions")
        row = next(s for s in listing["sessions"] if s["id"] == ok["id"])
        self.assertEqual(row["roots"], [str((ws / "proj").resolve())])

    def test_ls_lists_directories_only(self):
        # M9 security contract: the picker route never discloses file names
        # or contents — a file path is refused, files inside a dir are skipped.
        ws = self.root / "lsws"
        (ws / "sub").mkdir(parents=True)
        (ws / "secret.txt").write_text("do not disclose")
        code, d = self.get(f"/harness/ls?path={ws}")
        self.assertEqual(code, 200)
        self.assertEqual([e["name"] for e in d["dirs"]], ["sub"])
        self.assertNotIn("secret.txt", json.dumps(d))
        code, err = self.get(f"/harness/ls?path={ws / 'secret.txt'}")
        self.assertEqual(code, 400)                       # refuses non-dir disclosure
        self.assertIn("not a directory", err["error"]["message"])



class PluginRouteTests(RoutesBase):
    """ADR-0014 over HTTP: off -> 404; on -> scan/review/source/disable."""

    def setUp(self):
        super().setUp()
        from . import test_plugins as tp
        self.plugins_dir = self.root / "plugins"
        self.plugins_dir.mkdir()
        self.mgr.plugins_dir = self.plugins_dir
        self.GOOD = tp.GOOD

    def test_404_when_plugins_off(self):
        self.mgr.plugins_dir = None
        code, body = self.get("/harness/plugins")
        self.assertEqual(code, 404)
        self.assertIn("plugins are off", body["error"]["message"])
        code, _ = self.post("/harness/plugins/review", {"name": "p1", "sha256": "x"})
        self.assertEqual(code, 404)

    def test_scan_review_source_disable_flow(self):
        path = self.plugins_dir / "p1.py"
        path.write_text(self.GOOD, encoding="utf-8")
        code, body = self.get("/harness/plugins")
        self.assertEqual(code, 200)
        self.assertEqual(body["dir"], str(self.plugins_dir))
        entry = next(p for p in body["plugins"] if p["name"] == "p1")
        self.assertEqual(entry["state"], "quarantined")
        # source view before any review: full text, no diff
        code, v = self.get("/harness/plugins/source?name=p1")
        self.assertEqual(code, 200)
        self.assertIn("plugin_echo", v["source"])
        self.assertIsNone(v["diff"])
        # review with the wrong hash -> 409 (TOCTOU guard)
        code, _ = self.post("/harness/plugins/review", {"name": "p1", "sha256": "deadbeef"})
        self.assertEqual(code, 409)
        code, body = self.post("/harness/plugins/review", {"name": "p1", "sha256": entry["sha256"]})
        self.assertEqual(code, 200)
        _, body = self.get("/harness/plugins")
        self.assertEqual(next(p for p in body["plugins"] if p["name"] == "p1")["state"], "active")
        # the tools route gains the plugin's tool (preset editor)
        _, tools = self.get("/harness/tools")
        self.assertIn("plugin_echo", tools["tools"])
        # edit -> changed + diff vs the reviewed snapshot
        path.write_text(self.GOOD + "\n# edited\n", encoding="utf-8")
        _, body = self.get("/harness/plugins")
        self.assertEqual(next(p for p in body["plugins"] if p["name"] == "p1")["state"], "changed")
        code, v = self.get("/harness/plugins/source?name=p1")
        self.assertEqual(code, 200)
        self.assertTrue(v["diff"] and any(ln.startswith("+") for ln in v["diff"]))
        # disable -> quarantined again
        code, body = self.post("/harness/plugins/disable", {"name": "p1"})
        self.assertEqual(code, 200)
        self.assertTrue(body["disabled"])
        _, body = self.get("/harness/plugins")
        self.assertEqual(next(p for p in body["plugins"] if p["name"] == "p1")["state"], "quarantined")

    def test_plugin_route_validation(self):
        code, _ = self.post("/harness/plugins/review", {"name": "nope"})
        self.assertEqual(code, 400)          # sha256 required
        code, _ = self.get("/harness/plugins/source?name=../etc/passwd")
        self.assertEqual(code, 404)          # bad name never resolves

class StreamTests(RoutesBase):
    def read_sse(self, f, want: int, timeout: float = 10.0):
        """Read `want` data: frames from the SSE stream (comment frames ignored)."""
        import time
        rows, buf = [], b""
        deadline = time.time() + timeout
        while len(rows) < want and time.time() < deadline:
            line = f.readline()
            if not line:
                break
            buf += line
            if buf.endswith(b"\n\n"):
                text, buf = buf.decode("utf-8"), b""
                for line in text.splitlines():
                    if line.startswith("data: "):
                        rows.append(json.loads(line[6:]))
        return rows

    def open_stream(self, sid: str):
        return urllib.request.urlopen(self.base + f"/harness/stream?session={sid}", timeout=15)

    def test_stream_replays_then_streams_live(self):
        _, created = self.post("/harness/session", {})
        sid = created["id"]
        self.post(f"/harness/session/{sid}/message", {"content": "hello"})
        ev = self.wait_idle(sid)
        n = len(ev["events"])
        resp = self.open_stream(sid)
        self.assertEqual(resp.headers["Content-Type"], "text/event-stream")
        rows = self.read_sse(resp, n)
        self.assertEqual([r["type"] for r in rows], [r["type"] for r in ev["events"]])
        seqs = [r["seq"] for r in rows]
        self.assertEqual(seqs, sorted(seqs))
        # a second message arrives on the same open stream
        self.mgr.live(sid).loop.engine = scripted(
            [EngineResult(content="second", usage={"prompt_tokens": 3, "completion_tokens": 1})])()
        self.post(f"/harness/session/{sid}/message", {"content": "again"})
        more = []
        while "turn/end" not in [r["type"] for r in more]:
            more += self.read_sse(resp, 1)
        types = [r["type"] for r in more]
        self.assertIn("user/message", types)
        self.assertIn("turn/end", types)
        resp.close()

    def test_stream_approval_ask_and_decide(self):
        self.mgr._live.clear()
        self.mgr.engine_factory = scripted([
            EngineResult(tool_calls=[{"id": "c1", "name": "shell", "arguments": {"command": "echo hi"}}],
                         usage={"prompt_tokens": 3, "completion_tokens": 2}),
            EngineResult(content="ok", usage={"prompt_tokens": 4, "completion_tokens": 1}),
        ])
        _, created = self.post("/harness/session", {})
        sid = created["id"]
        resp = self.open_stream(sid)
        self.post(f"/harness/session/{sid}/message", {"content": "run it"})
        ask, seen = None, 0
        while ask is None and seen < 30:
            rows = self.read_sse(resp, seen + 1, timeout=10)
            seen = len(rows)
            ask = next((r for r in rows if r["type"] == "approval/ask"), None)
        assert ask is not None
        self.assertEqual(ask["data"]["call_id"], "c1")
        self.assertEqual(ask["seq"], -1)
        code, _ = self.post(f"/harness/session/{sid}/decide", {"call_id": "c1", "outcome": "allow-once"})
        self.assertEqual(code, 200)
        # the durable asked/decided rows follow on the same stream
        rest = self.read_sse(resp, 3, timeout=10)
        types = [r["type"] for r in rest]
        self.assertIn("approval/asked", types)
        self.assertIn("approval/decided", types)
        resp.close()

    def test_stream_bad_session(self):
        code, _ = self.get("/harness/stream")
        self.assertEqual(code, 400)
        code, _ = self.get("/harness/stream?session=nosuch")
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
