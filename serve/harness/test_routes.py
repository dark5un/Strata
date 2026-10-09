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
        self.svc.harness = self.mgr
        self.httpd = serve(self.svc, port=0)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._tmp.cleanup()

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
        self.svc.harness = None
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
        self.assertEqual(types[-1], "turn/end")
        code, listing = self.get("/harness/sessions")
        self.assertEqual([s["id"] for s in listing["sessions"]], [sid])

    def test_message_validation(self):
        _, created = self.post("/harness/session", {})
        code, _ = self.post(f"/harness/session/{created['id']}/message", {"content": "  "})
        self.assertEqual(code, 400)
        code, _ = self.post("/harness/session/nosuch/message", {"content": "x"})
        self.assertEqual(code, 404)

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
        code, _ = self.post(f"/harness/session/{sid}/policy", {"mode": "yolo"})
        self.assertEqual(code, 400)

    def test_decide_unknown_call_is_409(self):
        _, created = self.post("/harness/session", {})
        code, _ = self.post(f"/harness/session/{created['id']}/decide",
                            {"call_id": "nope", "outcome": "deny"})
        self.assertEqual(code, 409)

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
