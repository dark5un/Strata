"""serve/harness/test_manager.py — the live-session manager against a scripted engine.

    python -m unittest serve.harness.test_manager -v
"""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from . import events as ev
from .engine import EngineResult
from .manager import HarnessManager


def scripted(script):
    """engine factory: pops one EngineResult per call; empty -> plain stop."""
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


def wait_idle(mgr: HarnessManager, sid: str, timeout: float = 5.0) -> None:
    import time
    end = time.time() + timeout
    while time.time() < end:
        if not mgr.live(sid).busy:
            return
        time.sleep(0.02)
    raise TimeoutError(f"session {sid} still busy")


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _mgr(self, script):
        return HarnessManager(self.root / "sessions", scripted(script),
                              spill_dir=self.root / "spill", workspace_root=self.root)

    def test_send_completes_and_persists(self):
        mgr = self._mgr([EngineResult(content="hi", usage={"prompt_tokens": 2, "completion_tokens": 1})])
        sid = mgr.new()
        mgr.send(sid, "hello")
        wait_idle(mgr, sid)
        types = [r["type"] for r in mgr.live(sid).handle.events]
        self.assertEqual(types[0], ev.USER_MESSAGE)
        self.assertIn(ev.TURN_END, types)          # M7: a session/title row may land after it

    def test_tool_roundtrip_through_manager(self):
        script = [
            EngineResult(tool_calls=[{"id": "c1", "name": "read_file", "arguments": {"path": "f.txt"}}],
                         usage={"prompt_tokens": 3, "completion_tokens": 2}),
            EngineResult(content="read it", usage={"prompt_tokens": 4, "completion_tokens": 2}),
        ]
        (self.root / "f.txt").write_text("contents", encoding="utf-8")
        mgr = self._mgr(script)
        sid = mgr.new()
        mgr.send(sid, "read f.txt")
        wait_idle(mgr, sid)
        results = [r for r in mgr.live(sid).handle.events if r["type"] == ev.TOOL_RESULT]
        self.assertEqual(len(results), 1)
        self.assertIn("1|contents", results[0]["data"]["text"])

    def test_gated_tool_waits_for_decision(self):
        script = [
            EngineResult(tool_calls=[{"id": "c1", "name": "shell", "arguments": {"command": "echo x"}}],
                         usage={"prompt_tokens": 3, "completion_tokens": 2}),
            EngineResult(content="ok", usage={"prompt_tokens": 4, "completion_tokens": 1}),
        ]
        mgr = self._mgr(script)
        sid = mgr.new()
        mgr.send(sid, "run it")
        live = mgr.live(sid)
        # the worker blocks on the approval queue: an ask lands
        ask = live.approvals.get(timeout=3)
        self.assertEqual(ask["call_id"], "c1")
        mgr.decide(sid, "c1", "allow-once")
        wait_idle(mgr, sid)
        decided = [r for r in live.handle.events if r["type"] == ev.APPROVAL_DECIDED]
        self.assertEqual(decided[0]["data"]["outcome"], "allow-once")
        results = [r for r in live.handle.events if r["type"] == ev.TOOL_RESULT]
        self.assertIn("exit 0", results[0]["data"]["text"])
        self.assertIn("x", results[0]["data"]["text"])

    def test_deny_blocks_shell(self):
        script = [
            EngineResult(tool_calls=[{"id": "c1", "name": "shell", "arguments": {"command": "touch nope"}}],
                         usage={"prompt_tokens": 3, "completion_tokens": 2}),
            EngineResult(content="denied", usage={"prompt_tokens": 4, "completion_tokens": 1}),
        ]
        mgr = self._mgr(script)
        sid = mgr.new()
        mgr.send(sid, "do it")
        live = mgr.live(sid)
        live.approvals.get(timeout=3)
        mgr.decide(sid, "c1", "deny")
        wait_idle(mgr, sid)
        self.assertFalse((self.root / "nope").exists())

    def test_policy_allowlist_skips_the_ask(self):
        script = [
            EngineResult(tool_calls=[{"id": "c1", "name": "shell", "arguments": {"command": "echo q"}}],
                         usage={"prompt_tokens": 3, "completion_tokens": 2}),
            EngineResult(content="ok", usage={"prompt_tokens": 4, "completion_tokens": 1}),
        ]
        mgr = self._mgr(script)
        sid = mgr.new()
        mgr.set_policy(sid, "never", ["shell:echo*"])
        mgr.send(sid, "go")
        wait_idle(mgr, sid)
        asked = [r for r in mgr.live(sid).handle.events if r["type"] == ev.APPROVAL_ASKED]
        self.assertEqual(len(asked), 0)
        results = [r for r in mgr.live(sid).handle.events if r["type"] == ev.TOOL_RESULT]
        self.assertIn("q", results[0]["data"]["text"])

    def test_subscribe_replays_then_streams(self):
        script = [EngineResult(content="a", usage={"prompt_tokens": 1, "completion_tokens": 1})]
        mgr = self._mgr(script)
        sid = mgr.new()
        mgr.send(sid, "one")
        wait_idle(mgr, sid)
        q = mgr.subscribe(sid)
        replayed = []
        while not q.empty():
            replayed.append(q.get())
        self.assertEqual(replayed[0]["type"], ev.USER_MESSAGE)
        self.assertIn(ev.TURN_END, [r["type"] for r in replayed])   # M7: session/title may land last
        mgr.send(sid, "two")
        wait_idle(mgr, sid)
        streamed = []
        while not q.empty():
            streamed.append(q.get())
        self.assertTrue(any(r["type"] == ev.USER_MESSAGE and r["data"]["content"] == "two" for r in streamed))

    def test_busy_send_becomes_inbox_followup(self):
        gate = threading.Event()

        def slow_engine(messages, tools, on_event=None):
            gate.wait(5)
            return EngineResult(content="late", usage={})
        mgr = HarnessManager(self.root / "sessions", lambda: slow_engine,
                             spill_dir=self.root / "spill", workspace_root=self.root)
        sid = mgr.new()
        mgr.send(sid, "first")
        live = mgr.live(sid)
        import time
        end = time.time() + 2
        while not live.busy and time.time() < end:
            time.sleep(0.01)
        mgr.send(sid, "steer")
        inbox = live.loop.inbox
        if inbox is None:
            self.fail("loop must have an inbox")
        self.assertEqual(inbox.pending(), 1)
        gate.set()
        wait_idle(mgr, sid)

    def test_crash_in_engine_settles_turn(self):
        def factory():
            def engine(messages, tools, on_event=None):
                raise RuntimeError("boom")
            return engine
        mgr = HarnessManager(self.root / "sessions", factory,
                             spill_dir=self.root / "spill", workspace_root=self.root)
        sid = mgr.new()
        mgr.send(sid, "x")
        wait_idle(mgr, sid)
        last = mgr.live(sid).handle.events[-1]
        self.assertEqual(last["type"], ev.TURN_END)
        self.assertEqual(last["data"]["reason"], ev.REASON_ERROR)

    def test_resume_after_reopen_repairs(self):
        # simulate a crash: write an open turn directly, then attach
        mgr = self._mgr([EngineResult(content="ok", usage={})])
        sid = mgr.new()
        h = mgr.live(sid).handle
        h.append(ev.TURN_START, {})
        h.append(ev.USER_MESSAGE, {"content": "orphan"})
        h.flush()
        mgr._live.clear()
        live = mgr.live(sid)                    # _attach -> repair_interrupted
        last = live.handle.events[-1]
        self.assertEqual(last["type"], ev.TURN_END)
        self.assertEqual(last["data"]["reason"], ev.REASON_INTERRUPTED)

    def test_fork_via_manager(self):
        mgr = self._mgr([EngineResult(content="a", usage={})])
        sid = mgr.new()
        mgr.send(sid, "hello")
        wait_idle(mgr, sid)
        kid = mgr.fork(sid)
        self.assertNotEqual(kid, sid)
        self.assertEqual(mgr.live(kid).handle.header["parentSession"], sid)

    def test_list_sessions(self):
        mgr = self._mgr([EngineResult(content="a", usage={})])
        sid = mgr.new()
        mgr.send(sid, "x")
        wait_idle(mgr, sid)
        listing = mgr.list_sessions()
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["id"], sid)
        self.assertGreater(listing[0]["events"], 0)

    def test_session_title(self):
        # M6 fallback + M7 D10: the model names the session after its first
        # turn; the truncated first message is the fallback while there is none
        mgr = self._mgr([EngineResult(content="a", usage={}),
                         EngineResult(content="counting harness files", usage={})])
        sid = mgr.new()
        mgr.send(sid, "count the   files\nunder serve/harness please")
        wait_idle(mgr, sid)
        self.assertEqual(mgr.list_sessions()[0]["title"], "counting harness files")
        # a fork inherits the title
        kid = mgr.fork(sid)
        by_id = {s["id"]: s for s in mgr.list_sessions()}
        self.assertEqual(by_id[kid]["title"], "counting harness files")

    def test_session_title_falls_back_when_model_fails(self):
        # D10 is best-effort: a throwing title call leaves the truncation label
        script = [EngineResult(content="a", usage={})]

        def factory():
            def engine(messages, tools, on_event=None):
                if len(messages) == 1 and "title for this conversation" in str(messages[0].get("content", "")):
                    raise RuntimeError("title call exploded")
                if script:
                    return script.pop(0)
                return EngineResult(content="done", usage={})
            return engine
        mgr = HarnessManager(self.root / "sessions", factory,
                             spill_dir=self.root / "spill", workspace_root=self.root)
        sid = mgr.new()
        mgr.send(sid, "explain the sse parser")
        wait_idle(mgr, sid)
        self.assertEqual(mgr.list_sessions()[0]["title"], "explain the sse parser")

    def test_session_title_empty_before_first_message(self):
        mgr = self._mgr([EngineResult(content="a", usage={})])
        mgr.new()
        self.assertEqual(mgr.list_sessions()[0]["title"], "")


if __name__ == "__main__":
    unittest.main()
