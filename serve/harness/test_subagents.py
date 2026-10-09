"""serve/harness/test_subagents.py — in-process child sessions end-to-end (ADR-0008).

Scripted engines, no GPU: the parent's engine calls `delegate`, the child's
engine answers with a final message; settlement, budgets, cancel cascade and
the approval relay are all driven through the manager.

    python -m unittest serve.harness.test_subagents -v
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from . import events as ev
from .engine import EngineResult
from .manager import HarnessManager
from .subagents import MAX_CHILDREN_PER_SESSION, budget_check, fold_spawns
from .test_manager import scripted, wait_idle


def _calls(*specs):
    """EngineResult emitting tool calls: specs are (name, args)."""
    return EngineResult(
        content="",
        tool_calls=[{"id": f"c{i}", "name": n, "arguments": a} for i, (n, a) in enumerate(specs)],
        usage={"prompt_tokens": 1, "completion_tokens": 1},
    )


class SubagentTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _mgr(self, script):
        return HarnessManager(self.root / "sessions", scripted(script),
                              spill_dir=self.root / "spill", workspace_root=self.root)

    def test_delegate_spawns_child_and_settles_into_parent(self):
        # parent: one delegate call, then done. child: one final message.
        parent_script = [
            _calls(("delegate", {"task": "count the files", "preset": "default"})),
            EngineResult(content="wrapping up", usage={"prompt_tokens": 1, "completion_tokens": 1}),
        ]
        child_script = [EngineResult(content="there are 7 files", usage={"prompt_tokens": 1, "completion_tokens": 1})]

        calls = {"n": 0}

        def factory():
            script = parent_script if calls["n"] == 0 else child_script
            calls["n"] += 1
            return scripted(script)()

        mgr = HarnessManager(self.root / "sessions", factory,
                             spill_dir=self.root / "spill", workspace_root=self.root)
        sid = mgr.new()
        mgr.send(sid, "do the thing")
        # the child runs in parallel: wait for its settlement to land in the
        # parent's log, then for the settlement turn to finish
        import time
        deadline = time.time() + 10
        while time.time() < deadline:
            if any(r["type"] == ev.USER_MESSAGE and r["data"].get("source") == "subagent"
                   for r in mgr.live(sid).handle.events):
                break
            time.sleep(0.05)
        wait_idle(mgr, sid, timeout=10.0)

        # the child exists with the right header
        rows = mgr.live(sid).handle.events
        spawns = fold_spawns(rows)
        self.assertEqual(len(spawns), 1)
        child_id = spawns[0]["child"]
        assert child_id is not None
        child = mgr.live(child_id)
        self.assertEqual(child.handle.header["origin"], "subagent")
        self.assertEqual(child.handle.header["parentSession"], sid)
        self.assertEqual(child.handle.header["delegationDepth"], 1)

        # settlement rode back into the parent as a subagent-sourced message
        msgs = [r for r in mgr.live(sid).handle.events if r["type"] == ev.USER_MESSAGE]
        settled = [m for m in msgs if m["data"].get("source") == "subagent"]
        self.assertEqual(len(settled), 1)
        self.assertIn("there are 7 files", settled[0]["data"]["content"])
        self.assertIn(child_id, settled[0]["data"]["content"])

    def test_child_denied_delegate_tool(self):
        mgr = self._mgr([EngineResult(content="x", usage={})])
        sid = mgr.new()
        self.assertIn("delegate", mgr.live(sid).loop.tools)
        child = mgr.spawn_child(sid, "task", "default")[0]
        assert child is not None
        self.assertNotIn("delegate", mgr.live(child).loop.tools)   # ADR-0008: children do not fan out

    def test_child_budget_depth_and_count(self):
        rows = [{"type": ev.SUBAGENT_SPAWN, "data": {"child": f"c{i}", "preset": "default", "depth": 1}}
                for i in range(MAX_CHILDREN_PER_SESSION - 1)]
        self.assertIsNone(budget_check(rows, 0))
        rows.append({"type": ev.SUBAGENT_SPAWN, "data": {"child": "cX", "preset": "default", "depth": 1}})
        self.assertIn("children", budget_check(rows, 0) or "")
        self.assertIn("depth", budget_check([], 2) or "")

    def test_spawn_refusal_is_a_tool_error(self):
        # the budget refusal path: spawn_child at the cap returns (None, reason)
        mgr = self._mgr([_calls(("delegate", {"task": "t"})),
                         EngineResult(content="ok", usage={"prompt_tokens": 1, "completion_tokens": 1})])
        sid = mgr.new()
        # fake a deep header by spawning through the manager twice is heavy;
        # instead check spawn_child's refusal path directly at the cap
        for _ in range(MAX_CHILDREN_PER_SESSION):
            cid, refusal = mgr.spawn_child(sid, "t", "default")
            self.assertIsNone(refusal, cid)
        cid, refusal = mgr.spawn_child(sid, "t", "default")
        self.assertIsNone(cid)
        self.assertIn("budget", refusal or "")
        self.assertEqual(len(fold_spawns(mgr.live(sid).handle.events)), MAX_CHILDREN_PER_SESSION)

    def test_cancel_cascades_to_children(self):
        import threading
        import time

        def slow_engine(messages, tools, on_event=None):
            threading.Event().wait(1.0)
            return EngineResult(content="late", usage={"prompt_tokens": 1, "completion_tokens": 1})

        mgr = HarnessManager(self.root / "sessions", lambda: slow_engine,
                             spill_dir=self.root / "spill", workspace_root=self.root)
        sid = mgr.new()
        mgr.send(sid, "parent task")            # parent busy in a slow turn
        cid, refusal = mgr.spawn_child(sid, "child task", "default")
        assert cid is not None and refusal is None
        self.assertTrue(mgr.live(sid).busy)
        self.assertTrue(mgr.live(cid).busy)
        self.assertTrue(mgr.cancel(sid))        # cancelling the busy parent cascades (ADR-0008)
        # wait for the child's settle hook to run (it discards from children)
        deadline = time.time() + 10
        while time.time() < deadline and mgr.live(sid).children:
            time.sleep(0.05)
        self.assertFalse(mgr.live(sid).children)
        reasons = {r["data"].get("reason") for r in mgr.live(cid).handle.events if r["type"] == ev.TURN_END}
        self.assertIn("aborted", reasons)
        # the settle lands in the log (parent idle at settle) or the inbox
        # (parent still winding down its aborted turn); drain either way so
        # nothing is left running when the test ends
        settled = [r for r in mgr.live(sid).handle.events
                   if r["type"] == ev.USER_MESSAGE and r["data"].get("source") == "subagent"]
        if not settled:
            inbox = mgr.live(sid).loop.inbox
            self.assertIsNotNone(inbox)
            self.assertGreater(inbox.pending() if inbox else 0, 0)
            wait_idle(mgr, sid, timeout=10.0)
            mgr.send(sid, "drain")
            wait_idle(mgr, sid, timeout=10.0)
            settled = [r for r in mgr.live(sid).handle.events
                       if r["type"] == ev.USER_MESSAGE and r["data"].get("source") == "subagent"]
        self.assertEqual(len(settled), 1)

    def test_preset_restrictions_apply_to_child(self):
        # the bundled researcher preset denies shell: a child with it must not carry the tool
        mgr = self._mgr([EngineResult(content="x", usage={})])
        sid = mgr.new()
        cid, refusal = mgr.spawn_child(sid, "t", "researcher")
        self.assertIsNone(refusal)
        assert cid is not None
        tools = mgr.live(cid).loop.tools
        self.assertNotIn("shell", tools)
        self.assertIn("read_file", tools)

    def test_child_approval_relays_to_parent_stream(self):
        parent_script = [
            _calls(("delegate", {"task": "run it", "preset": "default"})),
            EngineResult(content="wrapping up", usage={"prompt_tokens": 1, "completion_tokens": 1}),
        ]
        child_script = [
            _calls(("shell", {"command": "echo hi"})),
            EngineResult(content="ran it", usage={"prompt_tokens": 1, "completion_tokens": 1}),
        ]

        calls = {"n": 0}

        def factory():
            script = parent_script if calls["n"] == 0 else child_script
            calls["n"] += 1
            return scripted(script)()

        mgr = HarnessManager(self.root / "sessions", factory,
                             spill_dir=self.root / "spill", workspace_root=self.root)
        sid = mgr.new()
        mgr.send(sid, "go")
        # the child's shell ask must appear in the PARENT's approvals queue
        parent = mgr.live(sid)
        ask = None
        import time
        end = time.time() + 8
        while time.time() < end and ask is None:
            try:
                ask = parent.approvals.get(timeout=0.2)
            except Exception:  # queue.Empty
                continue
        assert ask is not None
        self.assertEqual(ask["tool"], "shell")
        self.assertIn("session", ask)                       # tagged with the child id
        # deciding through the PARENT session resolves the child's ask
        ok = mgr.decide(sid, ask["call_id"], "allow-once")
        self.assertTrue(ok)
        # wait for the child's settlement to land in the parent's log, then
        # for that turn to finish, before the test ends (no worker outlives tearDown)
        deadline = time.time() + 10
        while time.time() < deadline:
            if any(r["type"] == ev.USER_MESSAGE and r["data"].get("source") == "subagent"
                   for r in mgr.live(sid).handle.events):
                break
            time.sleep(0.05)
        wait_idle(mgr, sid, timeout=10.0)
        wait_idle(mgr, ask["session"], timeout=10.0)


if __name__ == "__main__":
    unittest.main()
