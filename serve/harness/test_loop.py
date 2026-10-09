"""serve/harness/test_loop.py — the agent loop against a scripted fake engine.

    python -m unittest serve.harness.test_loop -v
"""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from . import events as ev
from .loop import Loop, ToolDefinition
from .session import create, derive_messages, open_session


def fake_engine(script):
    """script: list of EngineResult-like dicts, consumed one per step."""
    calls = {"n": 0}

    def engine(messages, tools, on_event=None):
        i = calls["n"]
        calls["n"] += 1
        if i >= len(script):
            return {"content": "done", "tool_calls": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        step = script[i]
        if isinstance(step, Exception):
            raise step
        return step
    return engine


def echo_tool(name="echo", safe=True):
    def execute(args, ctx):
        return {"text": f"echo:{args.get('v', '')}"}
    return ToolDefinition(name=name, description="echo", parameters={"type": "object", "properties": {}},
                          execute=execute, concurrency_safe=safe, needs_approval=False)


class LoopTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _loop(self, script, tools=None, **kw):
        h = create(self.dir)
        reg = {t.name: t for t in (tools or [echo_tool()])}
        return Loop(engine=fake_engine(script), tools=reg, session=h, **kw), h

    def test_plain_turn(self):
        lp, h = self._loop([{"content": "hello", "tool_calls": [], "usage": {}}])
        end = lp.send("hi")
        self.assertEqual(end["reason"], ev.REASON_COMPLETED)
        types = [r["type"] for r in h.events]
        self.assertEqual(types, [ev.USER_MESSAGE, ev.TURN_START, ev.STEP_START, ev.REQUEST_HEADER,
                                 ev.ASSISTANT_MESSAGE, ev.STEP_END, ev.TURN_END])

    def test_tool_roundtrip(self):
        script = [
            {"content": None, "tool_calls": [{"id": "c1", "name": "echo", "arguments": {"v": "x"}}], "usage": {}},
            {"content": "after", "tool_calls": [], "usage": {}},
        ]
        lp, h = self._loop(script)
        end = lp.send("go")
        self.assertEqual(end["reason"], ev.REASON_COMPLETED)
        results = [r for r in h.events if r["type"] == ev.TOOL_RESULT]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["data"]["text"], "echo:x")
        msgs = derive_messages(h)
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "tool", "assistant"])
        self.assertEqual(msgs[1]["tool_calls"], [{"id": "c1", "name": "echo", "arguments": {"v": "x"}}])

    def test_unknown_tool_gets_error_result(self):
        script = [
            {"content": None, "tool_calls": [{"id": "c1", "name": "nope", "arguments": {}}], "usage": {}},
            {"content": "ok", "tool_calls": [], "usage": {}},
        ]
        lp, h = self._loop(script)
        lp.send("go")
        results = [r for r in h.events if r["type"] == ev.TOOL_RESULT]
        self.assertTrue(results[0]["data"]["is_error"])

    def test_approval_deny_blocks_execution(self):
        ran = {"v": False}

        def spy(args, ctx):
            ran["v"] = True
            return {"text": "ran"}
        tool = ToolDefinition(name="shell", description="s", parameters={"type": "object"}, execute=spy)
        tool.needs_approval = True
        lp, h = self._loop([
            {"content": None, "tool_calls": [{"id": "c1", "name": "shell", "arguments": {}}], "usage": {}},
            {"content": "ok", "tool_calls": [], "usage": {}},
        ], tools=[tool], approval=lambda req: "deny")
        lp.send("go")
        self.assertFalse(ran["v"])
        results = [r for r in h.events if r["type"] == ev.TOOL_RESULT]
        self.assertIn("approval deny", results[0]["data"]["text"])
        asked = [r for r in h.events if r["type"] == ev.APPROVAL_ASKED]
        decided = [r for r in h.events if r["type"] == ev.APPROVAL_DECIDED]
        self.assertEqual((len(asked), len(decided)), (1, 1))

    def test_approval_fail_closed_on_throw(self):
        tool = ToolDefinition(name="shell", description="s", parameters={"type": "object"},
                              execute=lambda a, c: {"text": "x"})
        tool.needs_approval = True

        def boom(req):
            raise RuntimeError("answerer down")
        lp, h = self._loop([
            {"content": None, "tool_calls": [{"id": "c1", "name": "shell", "arguments": {}}], "usage": {}},
            {"content": "ok", "tool_calls": [], "usage": {}},
        ], tools=[tool], approval=boom)
        lp.send("go")
        decided = [r for r in h.events if r["type"] == ev.APPROVAL_DECIDED]
        self.assertEqual(decided[0]["data"]["outcome"], "unavailable")

    def test_no_answerer_denies_gated_tool(self):
        tool = ToolDefinition(name="shell", description="s", parameters={"type": "object"},
                              execute=lambda a, c: {"text": "should not run"})
        tool.needs_approval = True
        lp, h = self._loop([
            {"content": None, "tool_calls": [{"id": "c1", "name": "shell", "arguments": {}}], "usage": {}},
            {"content": "ok", "tool_calls": [], "usage": {}},
        ], tools=[tool])
        lp.send("go")
        decided = [r for r in h.events if r["type"] == ev.APPROVAL_DECIDED]
        self.assertEqual(decided[0]["data"]["outcome"], "unavailable")

    def test_engine_error_settles_turn(self):
        lp, h = self._loop([RuntimeError("engine exploded")])
        end = lp.send("go")
        self.assertEqual(end["reason"], ev.REASON_ERROR)
        self.assertIn("engine exploded", end["message"])
        # log is durable and brackets balanced
        h2 = open_session(self.dir, h.header["id"])
        self.assertEqual(h2.events[-1]["type"], ev.TURN_END)

    def test_conclude_turn_stops_early(self):
        def finisher(args, ctx):
            ctx.conclude_turn()
            return {"text": "final"}
        tool = ToolDefinition(name="finish", description="f", parameters={"type": "object"}, execute=finisher, needs_approval=False)
        script = [{"content": None, "tool_calls": [{"id": "c1", "name": "finish", "arguments": {}}], "usage": {}},
                  {"content": "SHOULD NOT HAPPEN", "tool_calls": [], "usage": {}}]
        lp, h = self._loop(script, tools=[tool])
        end = lp.send("go")
        self.assertEqual(end["reason"], ev.REASON_COMPLETED)
        self.assertTrue(end.get("concluded"))
        self.assertNotIn("SHOULD NOT HAPPEN", str([r["data"] for r in h.events]))

    def test_parallel_safe_tools_run_concurrently(self):
        barrier = threading.Barrier(2, timeout=5)

        def rendezvous(args, ctx):
            barrier.wait()          # both must be in-flight at once or this raises BrokenBarrier
            return {"text": "ok"}
        tool = ToolDefinition(name="par", description="p", parameters={"type": "object"},
                              execute=rendezvous, concurrency_safe=True, needs_approval=False)
        script = [{"content": None, "tool_calls": [
            {"id": "a", "name": "par", "arguments": {}}, {"id": "b", "name": "par", "arguments": {}}], "usage": {}},
            {"content": "done", "tool_calls": [], "usage": {}}]
        lp, h = self._loop(script, tools=[tool])
        lp.send("go")
        results = [r for r in h.events if r["type"] == ev.TOOL_RESULT]
        self.assertEqual(len(results), 2)
        self.assertTrue(all(not r["data"]["is_error"] for r in results))

    def test_unsafe_tools_are_barriers(self):
        order = []

        def slow(args, ctx):
            order.append("start")
            threading.Event().wait(0.15)
            order.append("end")
            return {"text": "x"}
        tool = ToolDefinition(name="slow", description="s", parameters={"type": "object"},
                              execute=slow, concurrency_safe=False, needs_approval=False)
        script = [{"content": None, "tool_calls": [
            {"id": "a", "name": "slow", "arguments": {}}, {"id": "b", "name": "slow", "arguments": {}}], "usage": {}},
            {"content": "done", "tool_calls": [], "usage": {}}]
        lp, h = self._loop(script, tools=[tool])
        lp.send("go")
        self.assertEqual(order, ["start", "end", "start", "end"])

    def test_tool_timeout_synthesizes_result(self):
        def hang(args, ctx):
            threading.Event().wait(5)
            return {"text": "never"}
        tool = ToolDefinition(name="hang", description="h", parameters={"type": "object"},
                              execute=hang, concurrency_safe=False, timeout_s=0.2, needs_approval=False)
        script = [{"content": None, "tool_calls": [{"id": "a", "name": "hang", "arguments": {}}], "usage": {}},
                  {"content": "recovered", "tool_calls": [], "usage": {}}]
        lp, h = self._loop(script, tools=[tool])
        lp.send("go")
        results = [r for r in h.events if r["type"] == ev.TOOL_RESULT]
        self.assertEqual(results[0]["data"]["outcome"], ev.TOOL_OUTCOME_UNKNOWN)
        self.assertTrue(results[0]["data"]["is_error"])

    def test_max_steps_blocks_runaway(self):
        script = [{"content": None, "tool_calls": [{"id": f"c{i}", "name": "echo", "arguments": {}}], "usage": {}}
                  for i in range(10)]
        lp, h = self._loop(script, max_steps_per_turn=3)
        end = lp.send("go")
        self.assertEqual(end["reason"], ev.REASON_BLOCKED)

    def test_inbox_steering_lands_next_step(self):
        from .inbox import Inbox
        inbox = Inbox()
        inbox.send("steer now", target="next-step", preset="steer")
        lp, h = self._loop([
            {"content": None, "tool_calls": [{"id": "c1", "name": "echo", "arguments": {}}], "usage": {}},
            {"content": "ok", "tool_calls": [], "usage": {}},
        ], inbox=inbox)
        lp.send("go")
        users = [r["data"] for r in h.events if r["type"] == ev.USER_MESSAGE]
        self.assertEqual([u["content"] for u in users], ["go", "steer now"])
        self.assertEqual(users[1]["source"], "steer")

    def test_request_header_precedes_every_step(self):
        lp, h = self._loop([{"content": "x", "tool_calls": [], "usage": {}}])
        lp.send("go")
        types = [r["type"] for r in h.events]
        self.assertLess(types.index(ev.STEP_START), types.index(ev.REQUEST_HEADER))
        self.assertLess(types.index(ev.REQUEST_HEADER), types.index(ev.ASSISTANT_MESSAGE))


if __name__ == "__main__":
    unittest.main()
