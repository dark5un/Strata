"""serve/harness/test_stats.py — the observability fold (ADR-0017, M11a).

Pure fold tests over hand-built event rows: turn brackets, tool
outcome buckets, approval pairing, compaction markers, the open-turn
case, and the meter-fold context column. No HTTP, no engine.

    python -m unittest serve.harness.test_stats -v
"""
from __future__ import annotations

import unittest

from . import events as ev
from .stats import fold_stats


def rows(spec: list[tuple[str, dict]]) -> list[dict]:
    """Build event rows with seq + a synthetic clock (1s per row)."""
    return [ev.envelope(t, d, i + 1, ts=1000.0 + i) for i, (t, d) in enumerate(spec)]


METRICS = {"engine": {"max_context": 1000}}   # context_pressure: estimate/1000


def metrics_fn():
    return METRICS


class FoldTests(unittest.TestCase):
    def test_empty(self):
        out = fold_stats([], metrics_fn)
        self.assertEqual(out["turns"], [])
        self.assertEqual(out["totals"]["turns"], 0)
        self.assertIsNone(out["totals"]["ctxEstimate"])

    def test_one_clean_turn(self):
        r = rows([
            (ev.TURN_START, {}),
            (ev.USER_MESSAGE, {"content": "do a thing"}),
            (ev.STEP_START, {"step": 1}),
            (ev.ASSISTANT_MESSAGE, {"content": "", "tool_calls": [{"id": "c1", "name": "shell"}],
                                    "usage": {"prompt_tokens": 120, "completion_tokens": 8}}),
            (ev.TOOL_CALL, {"call_id": "c1", "name": "shell", "arguments": {"command": "ls"}}),
            (ev.TOOL_RESULT, {"call_id": "c1", "text": "a b c", "is_error": False}),
            (ev.STEP_END, {"step": 1, "ms": 400}),
            (ev.STEP_START, {"step": 2}),
            (ev.ASSISTANT_MESSAGE, {"content": "done", "usage": {"prompt_tokens": 140, "completion_tokens": 3}}),
            (ev.STEP_END, {"step": 2, "ms": 300}),
            (ev.TURN_END, {"reason": ev.REASON_COMPLETED}),
        ])
        out = fold_stats(r, metrics_fn)
        self.assertEqual(len(out["turns"]), 1)
        t = out["turns"][0]
        self.assertEqual(t["reason"], "completed")
        self.assertFalse(t["open"])
        self.assertEqual(t["steps"], 2)
        self.assertEqual(t["tools"], {"ok": 1, "error": 0, "unknown": 0, "pending": 0})
        self.assertEqual(t["toolNames"][0]["name"], "shell")
        # meter fold: last usage anchor (140+3) + nothing after it
        self.assertEqual(t["ctxEstimate"], 143)
        self.assertEqual(t["ctxPct"], 14.3)
        self.assertEqual(out["totals"]["tokens"], {"prompt": 140, "completion": 3})
        self.assertEqual(out["totals"]["turns"], 1)

    def test_tool_outcome_buckets(self):
        r = rows([
            (ev.TURN_START, {}),
            (ev.TOOL_CALL, {"call_id": "a", "name": "shell", "arguments": {}}),
            (ev.TOOL_RESULT, {"call_id": "a", "text": "boom", "is_error": True}),
            (ev.TOOL_CALL, {"call_id": "b", "name": "write_file", "arguments": {}}),
            (ev.TOOL_RESULT, {"call_id": "b", "text": "", "is_error": False,
                              "outcome": ev.TOOL_OUTCOME_UNKNOWN}),
            (ev.TOOL_CALL, {"call_id": "c", "name": "read_file", "arguments": {}}),
            (ev.TOOL_RESULT, {"call_id": "c", "text": "x" * 5000, "is_error": False,
                              "spill": {"locator": "sp-1"}}),
            (ev.TOOL_CALL, {"call_id": "d", "name": "shell", "arguments": {}}),
            (ev.TURN_END, {"reason": ev.REASON_ABORTED}),
        ])
        t = fold_stats(r, metrics_fn)["turns"][0]
        self.assertEqual(t["tools"], {"ok": 1, "error": 1, "unknown": 1, "pending": 1})
        by = {e["callId"]: e for e in t["toolNames"]}
        self.assertEqual(by["a"]["outcome"], "error")
        self.assertEqual(by["b"]["outcome"], "unknown")
        self.assertEqual(by["c"]["outcome"], "ok")
        self.assertTrue(by["c"].get("spill"))
        self.assertEqual(by["d"]["outcome"], "pending")
        self.assertEqual(t["reason"], "aborted")

    def test_approvals_pair_by_call_id(self):
        r = rows([
            (ev.TURN_START, {}),
            (ev.APPROVAL_ASKED, {"call_id": "a", "tool": "shell", "preview": "rm -rf"}),
            (ev.APPROVAL_DECIDED, {"call_id": "a", "tool": "shell", "outcome": "deny", "asked": True}),
            (ev.APPROVAL_ASKED, {"call_id": "b", "tool": "shell", "preview": "ls"}),
            (ev.APPROVAL_DECIDED, {"call_id": "b", "tool": "shell", "outcome": "allow-once", "asked": True}),
            (ev.APPROVAL_DECIDED, {"call_id": "c", "tool": "read_file", "outcome": "yolo", "asked": False}),
            (ev.TURN_END, {"reason": ev.REASON_COMPLETED}),
        ])
        ap = fold_stats(r, metrics_fn)["turns"][0]["approvals"]
        self.assertEqual(ap, {"asked": 2, "allowed": 1, "denied": 1, "cancelled": 0, "auto": 1})

    def test_open_turn_folds_live(self):
        r = rows([
            (ev.TURN_START, {}),
            (ev.USER_MESSAGE, {"content": "hello"}),
            (ev.STEP_START, {"step": 1}),
        ])
        out = fold_stats(r, metrics_fn)
        self.assertEqual(len(out["turns"]), 1)
        t = out["turns"][0]
        self.assertTrue(t["open"])
        self.assertIsNone(t["reason"])
        # no usage anchor: fold_meter prices the whole surface (user message)
        self.assertGreaterEqual(t["ctxEstimate"], 1)

    def test_compaction_marker_on_turn(self):
        r = rows([
            (ev.TURN_START, {}),
            (ev.COMPACTION_START, {"startSeq": 1, "endSeq": 5}),
            (ev.COMPACTION_SUMMARY, {"content": "summary"}),
            (ev.COMPACTION_END, {"committed": True, "startSeq": 1, "endSeq": 5}),
            (ev.TURN_END, {"reason": ev.REASON_COMPLETED}),
        ])
        t = fold_stats(r, metrics_fn)["turns"][0]
        self.assertEqual(t["compaction"], {"startSeq": 1, "endSeq": 5})

    def test_uncommitted_compaction_not_marked(self):
        r = rows([
            (ev.TURN_START, {}),
            (ev.COMPACTION_START, {"startSeq": 1, "endSeq": 5}),
            (ev.COMPACTION_END, {"committed": False}),
            (ev.TURN_END, {"reason": ev.REASON_COMPLETED}),
        ])
        self.assertIsNone(fold_stats(r, metrics_fn)["turns"][0]["compaction"])

    def test_children_fold(self):
        r = rows([
            (ev.TURN_START, {}),
            (ev.SUBAGENT_SPAWN, {"child": "kid1", "preset": "default", "depth": 1}),
            (ev.TURN_END, {"reason": ev.REASON_COMPLETED}),
        ])
        kids = fold_stats(r, metrics_fn)["children"]
        self.assertEqual(kids, [{"id": "kid1", "preset": "default", "depth": 1}])

    def test_totals_across_turns(self):
        r = rows([
            (ev.TURN_START, {}),
            (ev.STEP_END, {"step": 3, "ms": 100}),
            (ev.TURN_END, {"reason": ev.REASON_COMPLETED}),
            (ev.TURN_START, {}),
            (ev.STEP_END, {"step": 1, "ms": 50}),
            (ev.TOOL_CALL, {"call_id": "a", "name": "shell", "arguments": {}}),
            (ev.TOOL_RESULT, {"call_id": "a", "text": "x", "is_error": False}),
            (ev.TURN_END, {"reason": ev.REASON_COMPLETED}),
        ])
        out = fold_stats(r, metrics_fn)
        self.assertEqual(out["totals"]["turns"], 2)
        self.assertEqual(out["totals"]["steps"], 4)
        self.assertEqual(out["totals"]["tools"]["ok"], 1)
        self.assertGreater(out["totals"]["ms"], 0)

    def test_no_metrics_fn_leaves_pct_none(self):
        r = rows([(ev.TURN_START, {}), (ev.TURN_END, {"reason": ev.REASON_COMPLETED})])
        t = fold_stats(r, None)["turns"][0]
        self.assertIsNotNone(t["ctxEstimate"])
        self.assertIsNone(t["ctxPct"])

    def test_unknown_events_ignored(self):
        r = rows([
            (ev.TURN_START, {}),
            ("future/event", {"whatever": True}),
            (ev.TURN_END, {"reason": ev.REASON_COMPLETED}),
        ])
        self.assertEqual(fold_stats(r, metrics_fn)["totals"]["turns"], 1)


if __name__ == "__main__":
    unittest.main()
