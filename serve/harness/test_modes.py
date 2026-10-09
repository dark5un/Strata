"""serve/harness/test_modes.py — M3: plan/goal modes, spill locator, meter-driven compaction.

    python -m unittest serve.harness.test_modes -v
"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from . import events as ev
from . import modes
from .engine import EngineResult
from .loop import Loop
from .manager import HarnessManager
from .session import create, derive_messages
from .spill import SpillStore
from .tools.modes import goal_tool, plan_mode_tool
from .tools.spill import spill_read_tool


class FoldTests(unittest.TestCase):
    def test_plan_fold(self):
        rows = [{"type": ev.PLAN_MODE, "data": {"active": True}},
                {"type": ev.PLAN_MODE, "data": {"active": False}}]
        self.assertFalse(modes.fold_plan(rows))
        self.assertTrue(modes.fold_plan(rows[:1]))
        self.assertFalse(modes.fold_plan([]))

    def test_goal_fold_and_rounds(self):
        rows = [{"type": ev.GOAL_CHANGE, "data": {"objective": "ship", "phase": "active", "maxRounds": 3}},
                {"type": ev.USER_MESSAGE, "data": {"content": "x", "source": "goal"}},
                {"type": ev.GOAL_CHANGE, "data": {"objective": "ship", "phase": "complete", "maxRounds": 3}}]
        goal = modes.fold_goal(rows)
        assert goal is not None
        self.assertEqual(goal["phase"], "complete")
        self.assertEqual(modes.goal_rounds_used(rows), 1)

    def test_bad_phase_fails_closed_to_active(self):
        goal = modes.fold_goal([{"type": ev.GOAL_CHANGE,
                                 "data": {"objective": "x", "phase": "nonsense", "maxRounds": 2}}])
        assert goal is not None
        self.assertEqual(goal["phase"], "active")

    def test_sections(self):
        self.assertIsNone(modes.plan_section(False))
        section = modes.plan_section(True)
        assert section is not None
        self.assertIn("PLAN MODE", section)
        self.assertIsNone(modes.goal_section(None))
        self.assertIsNone(modes.goal_section({"objective": "x", "phase": "paused", "maxRounds": 1}))
        section = modes.goal_section({"objective": "x", "phase": "active", "maxRounds": 1})
        assert section is not None
        self.assertIn("GOAL MODE", section)


class ModeToolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.h = create(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_plan_enter_exit(self):
        tool = plan_mode_tool()
        r = tool.execute({"action": "enter"}, _ctx(self.h))
        self.assertFalse(r.get("is_error"))
        self.assertTrue(modes.fold_plan(self.h.events))
        r = tool.execute({"action": "exit", "plan": "do it"}, _ctx(self.h))
        self.assertFalse(r.get("is_error"))
        self.assertFalse(modes.fold_plan(self.h.events))

    def test_plan_exit_needs_text(self):
        r = plan_mode_tool().execute({"action": "exit"}, _ctx(self.h))
        self.assertTrue(r["is_error"])

    def test_goal_tool_reuses_objective(self):
        tool = goal_tool()
        tool.execute({"phase": "active", "objective": "ship it"}, _ctx(self.h))
        r = tool.execute({"phase": "complete"}, _ctx(self.h))
        self.assertFalse(r.get("is_error"))
        goal = modes.fold_goal(self.h.events)
        assert goal is not None
        self.assertEqual(goal["phase"], "complete")

    def test_goal_bad_phase(self):
        r = goal_tool().execute({"phase": "frobnicate"}, _ctx(self.h))
        self.assertTrue(r["is_error"])


class SpillReadTests(unittest.TestCase):
    def test_pages_and_errors(self):
        with tempfile.TemporaryDirectory() as td:
            store = SpillStore(Path(td))
            big = "".join(f"line{i}\n" for i in range(5000))
            meta = store.save_text(big)
            tool = spill_read_tool(store)
            h = create(Path(td))
            r = tool.execute({"locator": meta["locator"], "offset": 0}, _ctx(h))
            self.assertFalse(r.get("is_error"))
            self.assertTrue(big.startswith(r["text"][:100]))
            self.assertIn("continue with offset=", r["text"])
            r2 = tool.execute({"locator": "spill://deadbeef"}, _ctx(h))
            self.assertTrue(r2.get("is_error"))
            self.assertTrue(tool.execute({"locator": "not-a-spill"}, _ctx(h)).get("is_error"))


class MeterCompactionTests(unittest.TestCase):
    """The loop compacts when the monitor says the window is filling (ADR-0006)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_pressure_triggers_prune_then_summary(self):
        h = create(self.dir)
        h.append(ev.TURN_START, {})
        h.append(ev.USER_MESSAGE, {"content": "old"})
        h.append(ev.ASSISTANT_MESSAGE, {"content": "a", "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
        h.append(ev.TURN_END, {"reason": ev.REASON_COMPLETED})
        h.append(ev.TURN_START, {})
        h.append(ev.TOOL_CALL, {"call_id": "c1", "name": "shell", "arguments": {}})
        h.append(ev.TOOL_RESULT, {"call_id": "c1", "text": "B" * 50_000, "is_error": False})
        h.append(ev.ASSISTANT_MESSAGE, {"content": "b", "usage": {"prompt_tokens": 12_000, "completion_tokens": 5}})
        h.append(ev.TURN_END, {"reason": ev.REASON_COMPLETED})
        h.flush()

        seen = {"n": 0}

        def engine(messages, tools, on_event=None):
            seen["n"] += 1
            return EngineResult(content="done", usage={"prompt_tokens": 12_500, "completion_tokens": 5})

        lp = Loop(engine=engine, tools={}, session=h,
                  metrics_fn=lambda: {"engine": {"max_context": 16_000}}, compact_at=0.5)
        lp.send("go on")
        types = [r["type"] for r in h.events]
        self.assertIn(ev.CONTEXT_PRESSURE, types)
        self.assertIn(ev.COMPACTION_START, types)      # prune alone wasn't enough -> summarize ran
        # the prune shadow node is durable in the raw log (the summary later replaces the span)
        shadows = [r for r in h.events if r["type"] == ev.TOOL_RESULT and "pruned_from_seq" in r["data"]]
        self.assertEqual(len(shadows), 1)
        self.assertIn("chars pruned", shadows[0]["data"]["text"])
        msgs = derive_messages(h)
        self.assertTrue(any(m["role"] == "user" and "[summary of earlier work]" in m["content"] for m in msgs))

    def test_low_pressure_no_compaction(self):
        h = create(self.dir)
        lp = Loop(engine=lambda m, t, on_event=None: EngineResult(content="x", usage={"prompt_tokens": 100,
                                                                                       "completion_tokens": 1}),
                  tools={}, session=h,
                  metrics_fn=lambda: {"engine": {"max_context": 100_000}}, compact_at=0.8)
        lp.send("go")
        self.assertNotIn(ev.CONTEXT_PRESSURE, [r["type"] for r in h.events])

    def test_system_sections_land_in_request(self):
        captured = {}
        h = create(self.dir)

        def engine(messages, tools, on_event=None):
            captured["messages"] = messages
            return EngineResult(content="x", usage={})
        lp = Loop(engine=engine, tools={}, session=h,
                  system_sections=[lambda: "PLAN SECTION"])
        lp.send("go")
        self.assertEqual(captured["messages"][0]["role"], "system")
        self.assertIn("PLAN SECTION", captured["messages"][0]["content"])


class GoalDriverTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_goal_rounds_queue_until_complete(self):
        box = {"i": 0}
        script = [
            EngineResult(tool_calls=[{"id": "g1", "name": "goal",
                                      "arguments": {"phase": "active", "objective": "count"}}],
                         usage={"prompt_tokens": 5, "completion_tokens": 2}),
            EngineResult(content="round one", usage={"prompt_tokens": 6, "completion_tokens": 2}),
            EngineResult(content="round two", usage={"prompt_tokens": 7, "completion_tokens": 2}),
            EngineResult(tool_calls=[{"id": "g2", "name": "goal",
                                      "arguments": {"phase": "complete", "objective": "count"}}],
                         usage={"prompt_tokens": 8, "completion_tokens": 2}),
            EngineResult(content="all done", usage={"prompt_tokens": 9, "completion_tokens": 2}),
        ]

        def factory():
            def engine(messages, tools, on_event=None):
                i = box["i"]
                box["i"] += 1
                return script[i] if i < len(script) else EngineResult(content="done", usage={})
            return engine
        mgr = HarnessManager(self.root / "sessions", factory,
                             spill_dir=self.root / "spill", workspace_root=self.root)
        sid = mgr.new()
        mgr.set_policy(sid, "never", ["goal"])          # goal tool is ungated anyway; keep quiet
        mgr.send(sid, "start counting")
        _wait_idle(mgr, sid, timeout=10)
        rows = mgr.live(sid).handle.events
        goal_msgs = [r for r in rows if r["type"] == ev.USER_MESSAGE and r["data"].get("source") == "goal"]
        self.assertGreaterEqual(len(goal_msgs), 1)      # the driver queued at least one round
        goal = modes.fold_goal(rows)
        assert goal is not None
        self.assertEqual(goal["phase"], "complete")

    def test_max_rounds_blocks(self):
        def factory():
            def engine(messages, tools, on_event=None):
                return EngineResult(content="spinning", usage={"prompt_tokens": 1, "completion_tokens": 1})
            return engine
        mgr = HarnessManager(self.root / "sessions", factory,
                             spill_dir=self.root / "spill", workspace_root=self.root)
        sid = mgr.new()
        mgr.set_mode(sid, goal={"objective": "never finish", "phase": "active", "maxRounds": 2})
        mgr.send(sid, "go")
        _wait_idle(mgr, sid, timeout=15)
        rows = mgr.live(sid).handle.events
        goal = modes.fold_goal(rows)
        assert goal is not None
        self.assertEqual(goal["phase"], "blocked")
        self.assertLessEqual(modes.goal_rounds_used(rows), 2)

    def test_paused_goal_does_not_drive(self):
        def factory():
            def engine(messages, tools, on_event=None):
                return EngineResult(content="ok", usage={"prompt_tokens": 1, "completion_tokens": 1})
            return engine
        mgr = HarnessManager(self.root / "sessions", factory,
                             spill_dir=self.root / "spill", workspace_root=self.root)
        sid = mgr.new()
        mgr.set_mode(sid, goal={"objective": "later", "phase": "paused", "maxRounds": 5})
        mgr.send(sid, "hello")
        _wait_idle(mgr, sid)
        rows = mgr.live(sid).handle.events
        self.assertEqual([r for r in rows if r["type"] == ev.USER_MESSAGE and r["data"].get("source") == "goal"], [])

    def test_mode_tools_are_ungated(self):
        # regression: goal/plan_mode/spill_read mutate only session state; an ask
        # for them would stall the goal driver waiting on a human
        box = {"i": 0}
        script = [
            EngineResult(tool_calls=[{"id": "g1", "name": "goal",
                                      "arguments": {"phase": "active", "objective": "quiet"}}],
                         usage={"prompt_tokens": 5, "completion_tokens": 2}),
            EngineResult(content="done", usage={"prompt_tokens": 6, "completion_tokens": 2}),
        ]

        def factory():
            def engine(messages, tools, on_event=None):
                i = box["i"]
                box["i"] += 1
                return script[i] if i < len(script) else EngineResult(content="done", usage={})
            return engine
        mgr = HarnessManager(self.root / "sessions", factory,
                             spill_dir=self.root / "spill", workspace_root=self.root)
        sid = mgr.new()
        mgr.send(sid, "set a goal")
        _wait_idle(mgr, sid, timeout=10)
        rows = mgr.live(sid).handle.events
        self.assertEqual([r for r in rows if r["type"] == ev.APPROVAL_ASKED], [])
        for name in ("goal", "plan_mode", "spill_read"):
            self.assertFalse(mgr.live(sid).loop.tools[name].needs_approval)


class SteerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_busy_send_with_steer_lands_next_step(self):
        gate = threading.Event()

        def factory():
            def engine(messages, tools, on_event=None):
                gate.wait(5)
                return EngineResult(content="late", usage={})
            return engine
        mgr = HarnessManager(self.root / "sessions", factory,
                             spill_dir=self.root / "spill", workspace_root=self.root)
        sid = mgr.new()
        mgr.send(sid, "first")
        live = mgr.live(sid)
        end = time.time() + 2
        while not live.busy and time.time() < end:
            time.sleep(0.01)
        mgr.send(sid, "interrupt!", target="steer")
        assert live.loop.inbox is not None
        self.assertEqual([m.preset for m in live.loop.inbox.drain("next-step")], ["steer"])
        gate.set()
        _wait_idle(mgr, sid)


def _ctx(handle):
    class C:
        session = handle
    return C()


def _wait_idle(mgr, sid, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if not mgr.live(sid).busy:
            return
        time.sleep(0.02)
    raise TimeoutError(f"session {sid} still busy")


if __name__ == "__main__":
    unittest.main()
