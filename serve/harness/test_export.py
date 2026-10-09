"""serve/harness/test_export.py — Markdown/JSONL export (M4).

    python -m unittest serve.harness.test_export -v
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from . import events as ev
from .export import to_jsonl, to_markdown
from .session import create


class ExportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.h = create(Path(self._tmp.name) / "sessions", "exp1", cwd="/tmp",
                        origin="subagent", parent_session="p1", delegation_depth=1, agent_preset="researcher")
        self.h.append(ev.USER_MESSAGE, {"content": "do the thing"})
        self.h.append(ev.ASSISTANT_MESSAGE, {"content": "working", "tool_calls": [
            {"id": "c1", "name": "shell", "arguments": {"command": "ls"}}]})
        self.h.append(ev.TOOL_CALL, {"call_id": "c1", "name": "shell", "arguments": {"command": "ls"}})
        self.h.append(ev.TOOL_RESULT, {"call_id": "c1", "text": "a\nb", "is_error": False})
        self.h.append(ev.APPROVAL_DECIDED, {"call_id": "c1", "outcome": "allow-once", "asked": True})
        self.h.append(ev.SUBAGENT_SPAWN, {"child": "kid", "preset": "default", "depth": 2})
        self.h.append(ev.USER_MESSAGE, {"content": "[subagent kid completed]\nreport", "source": "subagent"})
        self.h.append(ev.TURN_END, {"reason": ev.REASON_COMPLETED})

    def tearDown(self):
        self._tmp.cleanup()

    def test_markdown_shape(self):
        md = to_markdown(self.h)
        self.assertIn("# Harness session exp1", md)
        self.assertIn("origin: subagent", md)
        self.assertIn("parent: p1", md)
        self.assertIn("**You**: do the thing", md)
        self.assertIn("tool call `shell`", md)
        self.assertIn("approval: allow-once", md)
        self.assertIn("subagent spawned: kid", md)
        self.assertIn("**Subagent message**", md)
        self.assertNotIn("turn completed", md)          # clean turns leave no note

    def test_jsonl_roundtrips(self):
        lines = [ln for ln in to_jsonl(self.h).splitlines() if ln.strip()]
        header = json.loads(lines[0])
        self.assertEqual(header["type"], "session/header")
        self.assertEqual(header["data"]["id"], "exp1")
        rows = [json.loads(ln) for ln in lines[1:]]
        self.assertEqual([r["type"] for r in rows], [r["type"] for r in self.h.events])
        self.assertEqual([r["seq"] for r in rows], [r["seq"] for r in self.h.events])

    def test_markdown_error_result_flagged(self):
        self.h.append(ev.TOOL_RESULT, {"call_id": "c2", "text": "boom", "is_error": True})
        self.assertIn("(error)", to_markdown(self.h))


if __name__ == "__main__":
    unittest.main()
