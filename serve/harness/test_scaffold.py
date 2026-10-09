"""test_scaffold.py — the package skeleton's smoke test (M0).

Proves the harness package imports, its contract constants exist, and the
gate runner has something to run before the loop lands in the next slice.
"""
from __future__ import annotations

import unittest

import serve.harness as harness


class ScaffoldTest(unittest.TestCase):
    def test_format_version(self) -> None:
        self.assertEqual(harness.HARNESS_FORMAT_VERSION, 1)

    def test_package_is_stdlib_only(self) -> None:
        # ADR-0003/0005: the package must not pull third-party imports at load
        import sys

        third_party = [
            m for m in sys.modules
            if m.split(".")[0] in {"numpy", "requests", "yaml", "pydantic"}
        ]
        self.assertEqual(third_party, [])


if __name__ == "__main__":
    unittest.main()
