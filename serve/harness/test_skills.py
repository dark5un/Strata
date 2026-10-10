"""serve/harness/test_skills.py — the skills store (ADR-0018, M11c).

Pure store tests + the two tools over a temp dir: scan/index, the
preset gate, view/write validation, path-locking, and the mid-session
create -> viewable-same-gate property.

    python -m unittest serve.harness.test_skills -v
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from .skills import index_section, scan_skills, skill_view_tool, skill_write_tool

GOOD = """---
name: strata-ops
description: GPU slot rules and the resource gate.
---
Body line one.
"""


class _Ctx:
    class _S:
        events: list = []
    session = _S()


def seed(root: Path, name: str, text: str = GOOD) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(text.replace("strata-ops", name), encoding="utf-8")


class ScanTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_dir_is_silent(self):
        store, warns = scan_skills(self.root / "nope")
        self.assertEqual(store, [])
        self.assertEqual(warns, [])
        self.assertIsNone(index_section(store, warns))

    def test_good_skill(self):
        seed(self.root, "strata-ops")
        store, warns = scan_skills(self.root)
        self.assertEqual([s["name"] for s in store], ["strata-ops"])
        self.assertIn("GPU slot rules", store[0]["description"])
        self.assertEqual(warns, [])
        sec = index_section(store, warns) or ""
        self.assertIn("- strata-ops: GPU slot rules", sec)

    def test_malformed_fails_closed_per_skill(self):
        seed(self.root, "ok-one")
        (self.root / "bad-one").mkdir()
        (self.root / "bad-one" / "SKILL.md").write_text("no frontmatter here", encoding="utf-8")
        store, warns = scan_skills(self.root)
        self.assertEqual([s["name"] for s in store], ["ok-one"])
        self.assertEqual(len(warns), 1)
        sec = index_section(store, warns) or ""
        self.assertIn("warning:", sec)

    def test_name_must_match_dir(self):
        seed(self.root, "dir-name")
        (self.root / "dir-name" / "SKILL.md").write_text(GOOD, encoding="utf-8")  # frontmatter says strata-ops
        store, warns = scan_skills(self.root)
        self.assertEqual(store, [])
        self.assertIn("!=", warns[0])


class ToolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        seed(self.root, "strata-ops")
        (self.root / "strata-ops" / "references").mkdir()
        (self.root / "strata-ops" / "references" / "ports.md").write_text("11434 is the 5090", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_view_body(self):
        r = skill_view_tool(self.root, None).execute({"name": "strata-ops"}, _Ctx())
        self.assertFalse(r.get("is_error"))
        self.assertIn("Body line one.", r["text"])

    def test_view_supporting_file(self):
        r = skill_view_tool(self.root, None).execute({"name": "strata-ops", "file_path": "references/ports.md"}, _Ctx())
        self.assertIn("11434", r["text"])

    def test_view_traversal_refused(self):
        r = skill_view_tool(self.root, None).execute({"name": "strata-ops", "file_path": "../sessions/x"}, _Ctx())
        self.assertTrue(r.get("is_error"))

    def test_gate_hides_skill(self):
        r = skill_view_tool(self.root, {"other"}).execute({"name": "strata-ops"}, _Ctx())
        self.assertTrue(r.get("is_error"))
        self.assertIn("not available", r["text"])

    def test_write_then_view_same_gate(self):
        # ADR-0018 §2: a skill created mid-session is viewable immediately
        # (the gate is the preset allowlist, not an attach-time scan snapshot).
        t = skill_write_tool(self.root)
        r = t.execute({"name": "new-skill", "content":
                       "---\nname: new-skill\ndescription: fresh.\n---\nbody"}, _Ctx())
        self.assertFalse(r.get("is_error"))
        v = skill_view_tool(self.root, {"new-skill"}).execute({"name": "new-skill"}, _Ctx())
        self.assertIn("body", v["text"])

    def test_write_validation(self):
        t = skill_write_tool(self.root)
        self.assertTrue(t.execute({"name": "Bad Name", "content": GOOD}, _Ctx()).get("is_error"))
        self.assertTrue(t.execute({"name": "ok-name", "content": "no frontmatter"}, _Ctx()).get("is_error"))
        self.assertTrue(t.execute({"name": "ok-name", "content":
                                   "---\nname: other\ndescription: x\n---\n"}, _Ctx()).get("is_error"))
        self.assertTrue(t.execute({"name": "ok-name", "content": "x" * (33 * 1024)}, _Ctx()).get("is_error"))

    def test_write_replaces(self):
        t = skill_write_tool(self.root)
        r = t.execute({"name": "strata-ops", "content":
                       "---\nname: strata-ops\ndescription: updated.\n---\nnew body"}, _Ctx())
        self.assertIn("updated", r["text"])
        store, _ = scan_skills(self.root)
        self.assertEqual(store[0]["description"], "updated.")


class PresetGateTests(unittest.TestCase):
    def test_permits_skill(self):
        from .presets import Preset
        p = Preset(name="x", skills=["a"])
        self.assertTrue(p.permits_skill("a"))
        self.assertFalse(p.permits_skill("b"))
        q = Preset(name="y")
        self.assertTrue(q.permits_skill("anything"))


if __name__ == "__main__":
    unittest.main()
