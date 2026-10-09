"""serve/harness/test_presets.py — YAML presets: load, fail-closed, restrictions (ADR-0009).

    python -m unittest serve.harness.test_presets -v
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from .presets import DEFAULT_PRESET, Preset, list_presets, load_preset


class PresetTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name: str, text: str) -> None:
        (self.dir / f"{name}.yaml").write_text(text, encoding="utf-8")

    def test_default_needs_no_file(self):
        preset, warning = load_preset("default", self.dir)
        self.assertIs(preset, DEFAULT_PRESET)
        self.assertIsNone(warning)

    def test_unknown_fails_closed(self):
        preset, warning = load_preset("nope", self.dir)
        self.assertIs(preset, DEFAULT_PRESET)
        self.assertIn("unknown", warning or "")

    def test_malformed_fails_closed(self):
        self._write("bad", "tools: [not, a, mapping]")
        preset, warning = load_preset("bad", self.dir)
        self.assertIs(preset, DEFAULT_PRESET)
        self.assertIn("malformed", warning or "")

    def test_deny_and_allow(self):
        self._write("r", "name: r\npersona: be focused\ntools:\n  deny: [shell]\n  allow: [read_file, search]\n")
        preset, warning = load_preset("r", self.dir)
        self.assertIsNone(warning)
        self.assertEqual(preset.persona, "be focused")
        self.assertTrue(preset.permits("read_file"))
        self.assertTrue(preset.permits("search"))      # in the whitelist
        self.assertFalse(preset.permits("todo"))       # allow set = whitelist: everything else out
        self.assertFalse(preset.permits("shell"))

    def test_permits_default_open(self):
        p = Preset(name="x")
        self.assertTrue(p.permits("anything"))

    def test_bundled_researcher_loads(self):
        preset, warning = load_preset("researcher")   # the repo's presets/ dir
        self.assertIsNone(warning)
        self.assertFalse(preset.permits("shell"))
        self.assertTrue(preset.permits("read_file"))

    def test_list_presets(self):
        self._write("r", "name: r\npersona: hi\n")
        names = [p["name"] for p in list_presets(self.dir)]
        self.assertEqual(names, ["default", "r"])

    def test_list_skips_unreadable(self):
        self._write("broken", ": : :")
        names = [p["name"] for p in list_presets(self.dir)]
        self.assertNotIn("broken", names)


if __name__ == "__main__":
    unittest.main()
