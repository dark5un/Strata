"""test_scaffold.py — the package skeleton's smoke test (M0).

Proves the harness package imports, its contract constants exist, and the
gate runner has something to run before the loop lands in the next slice.
"""
from __future__ import annotations

import json
import subprocess
import sys
import unittest

import serve.harness as harness

_STDLIB = set(getattr(sys, "stdlib_module_names", ()))


class ScaffoldTest(unittest.TestCase):
    def test_format_version(self) -> None:
        self.assertEqual(harness.HARNESS_FORMAT_VERSION, 1)

    def test_loop_core_is_stdlib_only(self) -> None:
        # ADR-0003/0005: the loop core (events/session/loop/approval) must not
        # pull third-party imports. Presets (ADR-0009) may use PyYAML.
        import pathlib
        core = pathlib.Path(__file__).with_name("loop.py")
        if not core.exists():
            self.skipTest("loop core lands in the next slice")
        code = (
            "import sys, json\n"
            "import serve.harness.loop, serve.harness.session, "
            "serve.harness.approval, serve.harness.events\n"
            "std = set(sys.stdlib_module_names)\n"
            "roots = {m.split('.')[0] for m in sys.modules}\n"
            "third = sorted(r for r in roots if r not in std and not r.startswith('_') "
            "and r not in {'serve', 'typing_extensions'})\n"
            "print(json.dumps(third))\n"
        )
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(out.stdout), [])


if __name__ == "__main__":
    unittest.main()
