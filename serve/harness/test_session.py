"""serve/harness/test_session.py — session log, repair, fork, surface folding.

    python -m unittest serve.harness.test_session -v
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from . import events as ev
from .session import (
    SessionFormatUnsupported,
    create,
    derive_messages,
    fold_surface,
    fork,
    open_session,
    repair_interrupted,
)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_create_append_reload_roundtrip(self):
        h = create(self.dir, "s1", cwd="/work")
        h.append(ev.USER_MESSAGE, {"content": "hello"})
        h.append(ev.ASSISTANT_MESSAGE, {"content": "hi", "usage": {"prompt_tokens": 5, "completion_tokens": 2}})
        h.flush()
        h2 = open_session(self.dir, "s1")
        self.assertEqual(h2.header["id"], "s1")
        self.assertEqual([r["type"] for r in h2.events], [ev.USER_MESSAGE, ev.ASSISTANT_MESSAGE])
        self.assertEqual([r["seq"] for r in h2.events], [0, 1])

    def test_seq_monotonic_contiguous(self):
        h = create(self.dir)
        rows = [h.append(ev.USER_MESSAGE, {"content": str(i)}) for i in range(5)]
        self.assertEqual([r["seq"] for r in rows], list(range(5)))

    def test_torn_tail_truncated_on_open(self):
        h = create(self.dir, "s3")
        h.append(ev.USER_MESSAGE, {"content": "good"})
        h.flush()
        with open(h.path, "a", encoding="utf-8") as f:
            f.write('{"type": "user/mess')          # torn line
        h2 = open_session(self.dir, "s3")
        self.assertEqual(len(h2.events), 1)
        # the file on disk no longer carries the torn row
        self.assertEqual(len(h2.path.read_text(encoding="utf-8").splitlines()), 2)  # header + 1

    def test_unknown_event_types_kept_and_skipped(self):
        h = create(self.dir, "s4")
        h.append("future/thing", {"x": 1})
        h.append(ev.USER_MESSAGE, {"content": "ok"})
        h.flush()
        h2 = open_session(self.dir, "s4")
        self.assertEqual(len(h2.events), 2)
        self.assertEqual(derive_messages(h2), [{"role": "user", "content": "ok"}])

    def test_format_version_refusal(self):
        h = create(self.dir, "s5")
        h.flush()
        raw = h.path.read_text(encoding="utf-8")
        bad = json.loads(raw.splitlines()[0])
        bad["data"]["format_version"] = 999
        h.path.write_text(json.dumps(bad) + "\n", encoding="utf-8")
        with self.assertRaises(SessionFormatUnsupported):
            open_session(self.dir, "s5")

    def test_repair_interrupted_appends_closers(self):
        h = create(self.dir, "s6")
        h.append(ev.TURN_START, {})
        h.append(ev.USER_MESSAGE, {"content": "go"})
        h.append(ev.TOOL_CALL, {"call_id": "c1", "name": "shell", "arguments": {}})
        h.flush()
        closers = repair_interrupted(h)
        self.assertEqual([c["type"] for c in closers], [ev.TOOL_RESULT, ev.TURN_END])
        self.assertEqual(closers[0]["data"]["outcome"], ev.TOOL_NOT_STARTED)
        self.assertEqual(closers[1]["data"]["reason"], ev.REASON_INTERRUPTED)
        # derived history is provider-valid: every call has a result
        msgs = derive_messages(open_session(self.dir, "s6"))
        tool_msgs = [m for m in msgs if m["role"] == "tool"]
        self.assertEqual(len(tool_msgs), 1)

    def test_repair_balanced_session_noops(self):
        h = create(self.dir, "s7")
        h.append(ev.TURN_START, {})
        h.append(ev.TOOL_CALL, {"call_id": "c1", "name": "x", "arguments": {}})
        h.append(ev.TOOL_RESULT, {"call_id": "c1", "text": "", "is_error": False})
        h.append(ev.TURN_END, {"reason": ev.REASON_COMPLETED})
        h.flush()
        self.assertEqual(repair_interrupted(h), [])

    def test_fork_prefix_and_seed(self):
        h = create(self.dir, "parent")
        h.append(ev.TURN_START, {})
        h.append(ev.USER_MESSAGE, {"content": "one"})
        h.append(ev.ASSISTANT_MESSAGE, {"content": "1"})
        h.append(ev.TURN_END, {"reason": ev.REASON_COMPLETED})
        h.append(ev.TURN_START, {})
        h.append(ev.USER_MESSAGE, {"content": "two"})
        h.flush()
        child = fork(self.dir, h, boundary_seq=3, child_id="kid")
        self.assertEqual(child.header["parentSession"], "parent")
        self.assertEqual([r["data"].get("content") for r in child.events
                          if r["type"] == ev.USER_MESSAGE], ["one"])
        seed = child.events[-1]
        self.assertEqual(seed["type"], ev.SESSION_END_SEED)
        self.assertEqual(seed["data"]["inherited_event_count"], 4)

    def test_fork_refuses_open_turn(self):
        h = create(self.dir, "parent2")
        h.append(ev.TURN_START, {})
        h.append(ev.USER_MESSAGE, {"content": "one"})
        h.flush()
        with self.assertRaises(ValueError):
            fork(self.dir, h)

    def test_surface_replace_shadows_span(self):
        h = create(self.dir, "s8")
        h.append(ev.USER_MESSAGE, {"content": "old"})
        h.append(ev.ASSISTANT_MESSAGE, {"content": "older"})
        h.append(ev.COMPACTION_SUMMARY, {"content": "summary"},
                 surfaceOp={"op": "replace", "startSeq": 0, "endSeq": 1}, sourceEventSeqs=[0, 1])
        h.flush()
        h2 = open_session(self.dir, "s8")
        surface = fold_surface(h2.events)
        self.assertEqual([r["type"] for r in surface], [ev.COMPACTION_SUMMARY])
        msgs = derive_messages(h2)
        self.assertEqual(msgs, [{"role": "user", "content": "summary"}])
        # raw log untouched
        self.assertEqual(len(h2.events), 3)


if __name__ == "__main__":
    unittest.main()
