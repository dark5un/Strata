"""serve/harness/test_loop.py — the agent loop against a scripted fake engine.

    python -m unittest serve.harness.test_loop -v
"""
from __future__ import annotations

import json
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

    # ------------------------------------------------------------------ M7

    def test_reasoning_is_durable_but_not_replayed(self):
        # M7a-1: reasoning lands in the log for the human/export, and is NOT
        # fed back into the derived history (token cost; the Chat tab's server
        # strips reasoning-only turns the same way).
        lp, h = self._loop([{"content": "answer", "reasoning": "I checked twice", "tool_calls": [], "usage": {}}])
        lp.send("go")
        rows = [r for r in h.events if r["type"] == ev.ASSISTANT_MESSAGE]
        self.assertEqual(rows[0]["data"]["reasoning"], "I checked twice")
        msgs = derive_messages(h)
        self.assertNotIn("I checked twice", json.dumps(msgs))

    def test_stream_deltas_reach_the_sink(self):
        # M7a-3: the loop forwards engine deltas to on_stream (process-local, never logged)
        seen = []

        def engine(messages, tools, on_event=None):
            assert on_event is not None
            on_event({"kind": "reasoning", "text": "thin"})
            on_event({"kind": "content", "text": "hello"})
            return {"content": "hello", "tool_calls": [], "usage": {}}
        h = create(self.dir)
        lp = Loop(engine=engine, tools={}, session=h, on_stream=seen.append)
        lp.send("go")
        self.assertEqual([d["kind"] for d in seen], ["reasoning", "content"])
        self.assertNotIn("stream/delta", [r["type"] for r in h.events])   # never durable

    def test_request_header_carries_the_full_envelope(self):
        # M7b-1: every model request is reconstructable from log+code
        lp, h = self._loop([{"content": "x", "tool_calls": [], "usage": {}}])
        lp.sampling = {"temperature": 0.6}
        lp.send("go")
        hdr = [r for r in h.events if r["type"] == ev.REQUEST_HEADER][0]["data"]
        self.assertEqual(hdr["sampling"], {"temperature": 0.6})
        self.assertEqual([m["role"] for m in hdr["messages"]], ["user"])
        self.assertEqual(hdr["messages"][0]["content"], "go")
        self.assertEqual(len(hdr["tool_schemas_sha256"]), 64)

    def test_failed_model_call_appends_an_attempt(self):
        # M7b-2: a retried turn must not look identical to a clean one
        lp, h = self._loop([RuntimeError("engine exploded")])
        lp.send("go")
        attempts = [r for r in h.events if r["type"] == ev.ASSISTANT_ATTEMPT]
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["data"]["status"], "failed")
        self.assertIn("engine exploded", attempts[0]["data"]["error"])
        self.assertIsInstance(attempts[0]["data"]["ms"], int)

    def test_step_end_carries_wall_time(self):
        # M7b-3: per-step timing makes wandering turns diagnosable
        lp, h = self._loop([{"content": "x", "tool_calls": [], "usage": {}}])
        lp.send("go")
        step_end = [r for r in h.events if r["type"] == ev.STEP_END][0]["data"]
        self.assertIsInstance(step_end["ms"], int)

    def test_engine_timings_fold_into_step_end(self):
        lp, h = self._loop([{"content": "x", "tool_calls": [], "usage": {
            "prompt_tokens": 10, "completion_tokens": 2,
            "timings": {"prompt_ms": 40.0, "predicted_ms": 20.0}}}])
        lp.send("go")
        step_end = [r for r in h.events if r["type"] == ev.STEP_END][0]["data"]
        self.assertEqual(step_end["prompt_ms"], 40.0)
        self.assertEqual(step_end["decode_ms"], 20.0)

    def test_yolo_runs_the_tool_and_logs_it_unasked(self):
        # ADR-0010: yolo auto-allows gated tools, still audited as decided {yolo, asked: false}
        ran = {"v": False}

        def spy(args, ctx):
            ran["v"] = True
            return {"text": "ran"}
        tool = ToolDefinition(name="shell", description="s", parameters={"type": "object"}, execute=spy)
        tool.needs_approval = True
        from .approval import ApprovalBroker, ApprovalPolicy
        broker = ApprovalBroker(ApprovalPolicy("yolo"))
        lp, h = self._loop([
            {"content": None, "tool_calls": [{"id": "c1", "name": "shell", "arguments": {"command": "ls"}}], "usage": {}},
            {"content": "ok", "tool_calls": [], "usage": {}},
        ], tools=[tool], approval=broker.request)
        lp.send("go")
        self.assertTrue(ran["v"])
        self.assertEqual([r for r in h.events if r["type"] == ev.APPROVAL_ASKED], [])
        decided = [r for r in h.events if r["type"] == ev.APPROVAL_DECIDED]
        self.assertEqual(decided[0]["data"]["outcome"], "yolo")
        self.assertFalse(decided[0]["data"]["asked"])

    def test_approval_request_carries_full_args(self):
        # ADR-0010: the card and the matcher read the whole arguments, not a preview
        seen = {}

        def answerer(req):
            seen["args"] = req.args
            return "deny"
        tool = ToolDefinition(name="shell", description="s", parameters={"type": "object"},
                              execute=lambda a, c: {"text": "x"})
        tool.needs_approval = True
        long_cmd = "echo one && echo two && echo " + "z" * 400
        lp, h = self._loop([
            {"content": None, "tool_calls": [{"id": "c1", "name": "shell",
                                              "arguments": {"command": long_cmd}}], "usage": {}},
            {"content": "ok", "tool_calls": [], "usage": {}},
        ], tools=[tool], approval=answerer)
        lp.send("go")
        self.assertEqual(seen["args"], {"command": long_cmd})

    def test_pre_tool_hook_denies_before_the_approval_gate(self):
        # ADR-0013: a hook refusal is a normal error result; the human is never asked
        ran = {"v": False}
        asked = {"n": 0}

        def spy(args, ctx):
            ran["v"] = True
            return {"text": "ran"}
        tool = ToolDefinition(name="shell", description="s", parameters={"type": "object"}, execute=spy)
        tool.needs_approval = True

        def answerer(req):
            asked["n"] += 1
            return "allow-once"
        lp, h = self._loop([
            {"content": None, "tool_calls": [{"id": "c1", "name": "shell", "arguments": {"command": "rm -rf /"}}], "usage": {}},
            {"content": "ok", "tool_calls": [], "usage": {}},
        ], tools=[tool], approval=answerer)
        lp.pre_tool.append(lambda loop, name, args: "destructive path" if "rm -rf" in args.get("command", "") else None)
        lp.send("go")
        self.assertFalse(ran["v"])
        self.assertEqual(asked["n"], 0)
        results = [r for r in h.events if r["type"] == ev.TOOL_RESULT]
        self.assertIn("blocked by hook: destructive path", results[0]["data"]["text"])

    def test_post_tool_hook_observes_and_cannot_break_the_loop(self):
        seen = []
        lp, h = self._loop([
            {"content": None, "tool_calls": [{"id": "c1", "name": "echo", "arguments": {"v": "x"}}], "usage": {}},
            {"content": "ok", "tool_calls": [], "usage": {}},
        ])
        lp.post_tool.append(lambda loop, name, args, payload: seen.append((name, args, payload["text"])))
        lp.post_tool.append(lambda *a: 1 / 0)          # a throwing observer must not settle the turn as error
        turn = lp.send("go")
        self.assertEqual(turn["reason"], "completed")
        self.assertEqual(seen, [("echo", {"v": "x"}, "echo:x")])

    def test_pre_tool_hook_throwing_fails_open(self):
        ran = {"v": False}

        def spy(args, ctx):
            ran["v"] = True
            return {"text": "ran"}
        tool = ToolDefinition(name="echo2", description="s", parameters={"type": "object"}, execute=spy)
        tool.needs_approval = False
        lp, h = self._loop([
            {"content": None, "tool_calls": [{"id": "c1", "name": "echo2", "arguments": {}}], "usage": {}},
            {"content": "ok", "tool_calls": [], "usage": {}},
        ], tools=[tool])
        lp.pre_tool.append(lambda loop, name, args: (_ for _ in ()).throw(RuntimeError("hook down")))
        lp.send("go")
        self.assertTrue(ran["v"])                      # observers fail open; the gate stays the human's

    def test_meter_fold_publishes_a_context_delta_per_step(self):
        # ctx-pill 0% bug (2026-10-09): the pill reads the meter's fold (the
        # same number compaction uses), published at every step boundary as a
        # process-local stream delta — never a durable row.
        deltas = []
        lp, h = self._loop([{"content": "hi", "tool_calls": [], "usage": {}}],
                           metrics_fn=lambda: {"engine": {"max_context": 1000}},
                           on_stream=lambda d: deltas.append(d))
        lp.send("hello there")
        ctx = [d for d in deltas if d["kind"] == "context"]
        self.assertEqual(len(ctx), 1)                          # one step boundary
        self.assertGreater(ctx[0]["estimate"], 0)              # not the old 0
        self.assertLessEqual(ctx[0]["pressure"], 1.0)
        self.assertNotIn(ev.CONTEXT_PRESSURE, [r["type"] for r in h.events])  # below compact_at: no durable row


if __name__ == "__main__":
    unittest.main()
