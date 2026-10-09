"""serve/harness/test_policies.py — approval, meter, compaction, spill, inbox.

    python -m unittest serve.harness.test_policies -v
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from . import compaction
from . import events as ev
from .approval import ApprovalBroker, ApprovalPolicy, fold_policy
from .inbox import Inbox
from .meter import context_pressure, estimate_tokens, fold_meter
from .session import create, derive_messages, open_session
from .spill import SpillStore, spill_policy


class ApprovalTests(unittest.TestCase):
    def test_policy_modes(self):
        self.assertEqual(ApprovalPolicy("never").decide("shell", "rm -rf"), "deny")
        self.assertEqual(ApprovalPolicy("never", ["shell:echo*"]).decide("shell", "echo hi"), "allow-once")
        self.assertEqual(ApprovalPolicy("ask").decide("shell", "x"), "ask")
        self.assertEqual(ApprovalPolicy("allowlist", ["read*"]).decide("read_file", ""), "allow-once")
        self.assertEqual(ApprovalPolicy("allowlist", ["read*"]).decide("shell", ""), "ask")

    def test_broker_fail_closed(self):
        class Req:
            tool, args_preview = "shell", "rm"
        self.assertEqual(ApprovalBroker(ApprovalPolicy("ask")).request(Req()), "unavailable")
        self.assertEqual(ApprovalBroker(ApprovalPolicy("never")).request(Req()), "deny")

    def test_broker_outcome_validation(self):
        class Req:
            tool, args_preview = "shell", "ls"
        broker = ApprovalBroker(ApprovalPolicy("ask"), answerer=lambda r: "yes-please")
        self.assertEqual(broker.request(Req()), "unavailable")

    def test_fold_policy_from_log(self):
        h = create(Path(tempfile.mkdtemp()))
        h.append("approval/policy", {"mode": "allowlist", "allowlist": ["shell:echo*"]})
        policy = fold_policy(h.events)
        self.assertEqual(policy.mode, "allowlist")
        self.assertEqual(policy.decide("shell", "echo 1"), "allow-once")


class MeterTests(unittest.TestCase):
    def test_estimate_and_anchor(self):
        rows = [{"type": ev.ASSISTANT_MESSAGE, "seq": 5,
                 "data": {"content": "x", "usage": {"prompt_tokens": 100, "completion_tokens": 20}}},
                {"type": ev.TOOL_RESULT, "seq": 6, "data": {"text": "y" * 400}}]
        m = fold_meter(rows)
        self.assertEqual(m["prompt_estimate"], 100 + 20 + 100)   # anchor + 400/4
        self.assertEqual(m["anchor"]["seq"], 5)

    def test_pressure_from_monitor(self):
        metrics = {"engine": {"max_context": 1000}}
        self.assertAlmostEqual(context_pressure(metrics, 500), 0.5)
        self.assertEqual(context_pressure(metrics, 5000), 1.0)
        self.assertEqual(context_pressure(None, 500), 0.0)

    def test_estimate_tokens_floor(self):
        self.assertEqual(estimate_tokens(""), 1)


class CompactionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_prune_shadows_without_rewriting(self):
        h = create(self.dir, "p1")
        h.append(ev.TOOL_CALL, {"call_id": "c1", "name": "shell", "arguments": {}})
        h.append(ev.TOOL_RESULT, {"call_id": "c1", "text": "A" * 10_000, "is_error": False})
        h.flush()
        n = compaction.prune_tool_results(h)
        self.assertEqual(n, 1)
        h2 = open_session(self.dir, "p1")
        self.assertEqual(len(h2.events), 3)                     # raw log grew, never shrank (header excluded)
        msgs = derive_messages(h2)
        tool_msg = [m for m in msgs if m["role"] == "tool"][-1]
        self.assertIn("chars pruned", tool_msg["content"])
        self.assertLess(len(tool_msg["content"]), 5000)

    def test_pair_safe_start_widens_span(self):
        h = create(self.dir, "p2")
        h.append(ev.TOOL_CALL, {"call_id": "c1", "name": "x", "arguments": {}})   # seq 0
        h.append(ev.TOOL_RESULT, {"call_id": "c1", "text": "r", "is_error": False})  # seq 1
        self.assertEqual(compaction._pair_safe_start(h, 1), 0)

    def test_summarize_commits_bracket(self):
        h = create(self.dir, "p3")
        for i in range(3):
            h.append(ev.TURN_START, {})
            h.append(ev.USER_MESSAGE, {"content": f"t{i}"})
            h.append(ev.ASSISTANT_MESSAGE, {"content": f"a{i}"})
            h.append(ev.TURN_END, {"reason": ev.REASON_COMPLETED})
        h.flush()

        def engine(messages, tools):
            return {"content": "SUMMARY", "tool_calls": [], "usage": {}}
        self.assertTrue(compaction.summarize_oldest(h, engine, keep_last_turns=2))
        h2 = open_session(self.dir, "p3")
        self.assertIsNone(compaction.orphaned_bracket(h2))
        msgs = derive_messages(h2)
        self.assertIn("SUMMARY", msgs[0]["content"])           # lands at the span's start
        self.assertEqual(len(msgs), 5)                          # summary + last turn (3) + user seed
        self.assertEqual(msgs[1]["content"], "t1")

    def test_orphaned_bracket_detected(self):
        h = create(self.dir, "p4")
        h.append(ev.COMPACTION_START, {"startSeq": 0, "endSeq": 1})
        h.flush()
        h2 = open_session(self.dir, "p4")
        self.assertIsNotNone(compaction.orphaned_bracket(h2))


class SpillTests(unittest.TestCase):
    def test_spill_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            store = SpillStore(Path(td))
            big = "x" * 30_000
            out = spill_policy(big, store)
            self.assertIn("spilled to spill://", out["text"])
            self.assertLess(len(out["text"]), 9000)
            self.assertEqual(store.read(out["spill"]["locator"]), big)

    def test_small_result_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            out = spill_policy("short", SpillStore(Path(td)))
            self.assertEqual(out, {"text": "short"})


class InboxTests(unittest.TestCase):
    def test_addresses_and_presets(self):
        ib = Inbox()
        ib.send("a", "next-turn", "followup")
        ib.send("b", "next-step", "steer")
        ib.send("c", "next-step", "inject")
        self.assertEqual(ib.pending(), 3)
        self.assertEqual([m.content for m in ib.drain("next-step")], ["b", "c"])
        self.assertEqual(ib.drain("next-step"), [])

    def test_bad_address_rejected(self):
        ib = Inbox()
        with self.assertRaises(ValueError):
            ib.send("x", "nowhere", "followup")


if __name__ == "__main__":
    unittest.main()
