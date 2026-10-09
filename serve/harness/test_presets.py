"""serve/harness/test_presets.py — YAML presets: load, fail-closed, restrictions (ADR-0009).

    python -m unittest serve.harness.test_presets -v
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from .presets import DEFAULT_PRESET, Preset, delete_preset, list_presets, load_preset, save_preset


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

    def test_user_dir_wins_over_bundled(self):
        # ADR-0011: two dirs, user first
        user, bundled = self.dir / "user", self.dir / "bundled"
        user.mkdir()
        bundled.mkdir()
        (bundled / "p.yaml").write_text("name: p\npersona: bundled\n", encoding="utf-8")
        (user / "p.yaml").write_text("name: p\npersona: user\n", encoding="utf-8")
        import os
        os.environ["STRATA_HARNESS_PRESET_DIR"] = str(user)
        try:
            preset, warning = load_preset("p")
            self.assertIsNone(warning)
            self.assertEqual(preset.persona, "user")
            entries = {p["name"]: p for p in list_presets()}
            self.assertEqual(entries["p"]["source"], "user")
            self.assertEqual(entries["researcher"]["source"], "bundled")   # the bundled example still lists
        finally:
            del os.environ["STRATA_HARNESS_PRESET_DIR"]

    def test_save_and_delete_roundtrip(self):
        path = save_preset(self.dir, "mine", persona="be careful", allow=["read_file", "search"],
                           deny=["shell"], description="my preset")
        self.assertTrue(path.exists())
        preset, warning = load_preset("mine", self.dir)
        self.assertIsNone(warning)
        self.assertEqual(preset.persona, "be careful")
        self.assertEqual(preset.description, "my preset")
        self.assertTrue(preset.permits("read_file"))
        self.assertFalse(preset.permits("shell"))
        self.assertTrue(delete_preset(self.dir, "mine"))
        preset, warning = load_preset("mine", self.dir)
        self.assertIs(preset, DEFAULT_PRESET)
        self.assertIn("unknown", warning or "")

    def test_bad_names_rejected(self):
        for bad in ("", "default", "a/b", "..", "x" * 65, "na me"):
            with self.assertRaises(ValueError):
                save_preset(self.dir, bad, persona=None, allow=[], deny=[])
            with self.assertRaises(ValueError):
                delete_preset(self.dir, bad)

    def test_edit_bundled_shadows_via_user_copy(self):
        # the UI's duplicate-to-user flow: same name, user dir wins (ADR-0011)
        import os
        os.environ["STRATA_HARNESS_PRESET_DIR"] = str(self.dir)
        try:
            save_preset(None, "researcher", persona="my version", allow=[], deny=["delegate"])
            preset, warning = load_preset("researcher")
            self.assertIsNone(warning)
            self.assertEqual(preset.persona, "my version")
            self.assertTrue(preset.permits("shell"))     # the bundled deny[shell] is gone: user copy is the truth
        finally:
            del os.environ["STRATA_HARNESS_PRESET_DIR"]


if __name__ == "__main__":
    unittest.main()
