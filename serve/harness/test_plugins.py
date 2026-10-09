"""serve/harness/test_plugins.py — the ADR-0014 gates: hash-reviewed load,
quarantine, TOCTOU review, disable, contract failures, and the manager's
write-gate wiring (protected dirs + plugin chips)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from serve.harness import plugins as pl
from serve.harness.approval import ApprovalPolicy, targets_protected

GOOD = '''
from serve.harness.loop import ToolDefinition

def tools():
    return [ToolDefinition(name="plugin_echo", description="echo",
                           parameters={"type": "object", "properties": {}},
                           execute=lambda args, ctx: {"text": "echo", "is_error": False})]

def hooks():
    return {"pre_tool": [lambda loop, name, args: "no plugin_echo" if name == "plugin_echo" else None]}
'''

BAD_HOOKS = '''
def hooks():
    return {"not_a_hook": [lambda: None]}
'''

BOOM = 'raise RuntimeError("import exploded")\n'


def scripted_engine():
    def factory():
        def engine(messages, tools, on_event=None):
            from serve.harness.engine import EngineResult
            return EngineResult(content="done", usage={"prompt_tokens": 1, "completion_tokens": 1})
        return engine
    return factory


class _DirCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "plugins"
        self.dir.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, name: str, text: str) -> Path:
        p = self.dir / f"{name}.py"
        p.write_text(text, encoding="utf-8")
        return p


class ScanReview(_DirCase):
    def test_quarantined_never_imported(self):
        self.write("p1", GOOD)
        self.assertEqual([s["state"] for s in pl.scan(self.dir)], ["quarantined"])
        loaded, failures = pl.load_plugins(self.dir)
        self.assertEqual((loaded, failures), ([], []))       # the load gate holds

    def test_review_then_active(self):
        path = self.write("p1", GOOD)
        sha = pl._sha(path.read_bytes())
        r = pl.review(self.dir, "p1", sha)
        self.assertTrue(r["ok"])
        self.assertEqual([s["state"] for s in pl.scan(self.dir)], ["active"])
        loaded, failures = pl.load_plugins(self.dir)
        self.assertEqual(failures, [])
        self.assertEqual([t.name for t in loaded[0].tools], ["plugin_echo"])
        self.assertEqual(list(loaded[0].hooks), ["pre_tool"])

    def test_review_toctou(self):
        path = self.write("p1", GOOD)
        stale = pl._sha(path.read_bytes())
        path.write_text(GOOD + "\n# edited\n", encoding="utf-8")
        r = pl.review(self.dir, "p1", stale)
        self.assertFalse(r["ok"])                            # exact-bytes approval only
        self.assertEqual([s["state"] for s in pl.scan(self.dir)], ["quarantined"])

    def test_edit_requarantines(self):
        path = self.write("p1", GOOD)
        pl.review(self.dir, "p1", pl._sha(path.read_bytes()))
        path.write_text(GOOD.replace("echo", "shout"), encoding="utf-8")
        self.assertEqual([s["state"] for s in pl.scan(self.dir)], ["changed"])
        loaded, _ = pl.load_plugins(self.dir)
        self.assertEqual(loaded, [])                         # changed: fail closed

    def test_disable_quarantines(self):
        path = self.write("p1", GOOD)
        pl.review(self.dir, "p1", pl._sha(path.read_bytes()))
        self.assertTrue(pl.disable(self.dir, "p1"))
        self.assertEqual([s["state"] for s in pl.scan(self.dir)], ["quarantined"])

    def test_corrupt_manifest_fails_closed(self):
        (self.dir / pl.MANIFEST).write_text("{not json", encoding="utf-8")
        self.write("p1", GOOD)
        self.assertEqual([s["state"] for s in pl.scan(self.dir)], ["quarantined"])

    def test_source_view_diff(self):
        path = self.write("p1", GOOD)
        pl.review(self.dir, "p1", pl._sha(path.read_bytes()))
        path.write_text(GOOD + "\n# a new line\n", encoding="utf-8")
        v = pl.source_view(self.dir, "p1")
        assert v is not None
        self.assertIn("# a new line", v["source"])
        assert v["diff"] is not None
        self.assertTrue(any(ln.startswith("+") for ln in v["diff"]))
        self.assertIn("reviewed", "\n".join(v["diff"]))

    def test_bad_names_skipped(self):
        self.write("_private", GOOD)
        self.write("ok-1", GOOD)
        self.assertEqual([s["name"] for s in pl.scan(self.dir)], ["ok-1"])


class LoadFailures(_DirCase):
    def _activate(self, name: str, text: str) -> None:
        path = self.write(name, text)
        pl.review(self.dir, name, pl._sha(path.read_bytes()))

    def test_import_error_isolated(self):
        self._activate("boom", BOOM)
        self._activate("good", GOOD)
        loaded, failures = pl.load_plugins(self.dir)
        self.assertEqual([f["name"] for f in failures], ["boom"])
        self.assertIn("import exploded", failures[0]["error"])
        self.assertEqual([p.name for p in loaded], ["good"])   # others continue

    def test_bad_hook_key_fails_plugin(self):
        self._activate("p", BAD_HOOKS)
        loaded, failures = pl.load_plugins(self.dir)
        self.assertEqual(loaded, [])
        self.assertIn("unknown hook key", failures[0]["error"])


class WriteGate(unittest.TestCase):
    """ADR-0014 gate 2: protected dirs force the ask under every mode."""

    def test_yolo_still_asks_for_plugin_writes(self):
        pol = ApprovalPolicy(mode="yolo", protected_dirs=[Path("/home/x/plugins")])
        self.assertEqual(pol.decide("write_file", {"path": "/home/x/plugins/p.py", "content": "x"}), "ask")
        self.assertEqual(pol.decide("edit_file", {"path": "/home/x/plugins/p.py"}), "ask")
        self.assertEqual(pol.decide("write_file", {"path": "/home/x/other.py"}), "yolo")
        self.assertEqual(pol.decide("shell", {"command": "rm -rf /home/x/plugins"}), "yolo")  # not a file tool

    def test_allowlist_cannot_match_plugin_writes(self):
        pol = ApprovalPolicy(mode="allowlist", allowlist=["write_file:*"],
                             protected_dirs=[Path("/home/x/plugins")])
        self.assertEqual(pol.decide("write_file", {"path": "/home/x/plugins/p.py"}), "ask")

    def test_relative_escape_still_caught(self):
        self.assertTrue(targets_protected("write_file", {"path": "/home/x/plugins/../plugins/p.py"},
                                          [Path("/home/x/plugins")]))

    def test_no_protected_dirs_unchanged(self):
        pol = ApprovalPolicy(mode="yolo")
        self.assertEqual(pol.decide("write_file", {"path": "/home/x/plugins/p.py"}), "yolo")


class ManagerWiring(_DirCase):
    """attach-time wiring: plugin tools in the registry, hooks on the loop,
    protected dirs on the policy, the chip flags on the channel."""

    def setUp(self) -> None:
        super().setUp()
        from serve.harness.manager import HarnessManager
        self.mgr = HarnessManager(Path(self._tmp.name) / "sessions", scripted_engine(),
                                  workspace_root=Path(self._tmp.name), plugins_dir=self.dir)

    def test_quarantined_plugin_absent(self):
        self.write("p1", GOOD)
        sid = self.mgr.new()
        live = self.mgr.live(sid)
        self.assertNotIn("plugin_echo", live.loop.tools)
        self.assertEqual(live.loop.pre_tool, [])
        assert live.channel is not None
        self.assertEqual(live.channel.plugin_tools, set())

    def test_reviewed_plugin_registers_and_hooks(self):
        path = self.write("p1", GOOD)
        pl.review(self.dir, "p1", pl._sha(path.read_bytes()))
        sid = self.mgr.new()
        live = self.mgr.live(sid)
        self.assertIn("plugin_echo", live.loop.tools)
        self.assertEqual(len(live.loop.pre_tool), 1)
        assert live.channel is not None
        self.assertEqual(live.channel.plugin_tools, {"plugin_echo"})
        self.assertIn("plugin_echo", self.mgr.active_plugin_tools())
        # the pre_tool hook denies its own tool (ADR-0013 semantics ride through)
        reason = live.loop.pre_tool[0](live.loop, "plugin_echo", {})
        self.assertEqual(reason, "no plugin_echo")

    def test_protected_dirs_on_policy(self):
        sid = self.mgr.new()
        live = self.mgr.live(sid)
        broker = live.loop._broker  # type: ignore[attr-defined]
        self.assertEqual(broker.policy.protected_dirs, [self.dir])
        self.mgr.set_policy(sid, "yolo", [])
        broker = live.loop._broker  # type: ignore[attr-defined]
        self.assertEqual(broker.policy.protected_dirs, [self.dir])  # survives a mode change
        self.assertEqual(broker.policy.decide("write_file",
                                              {"path": str(self.dir / "x.py")}), "ask")

    def test_plugin_tool_collision_fails_plugin(self):
        collide = GOOD.replace("plugin_echo", "shell")
        path = self.write("p1", collide)
        pl.review(self.dir, "p1", pl._sha(path.read_bytes()))
        sid = self.mgr.new()
        live = self.mgr.live(sid)
        self.assertEqual(live.loop.tools["shell"].name, "shell")   # the built-in stays
        self.assertIn("collides", self.mgr.plugin_failures()[0]["error"])

    def test_plugins_off_by_default(self):
        from serve.harness.manager import HarnessManager
        mgr = HarnessManager(Path(self._tmp.name) / "s2", scripted_engine(),
                             workspace_root=Path(self._tmp.name))
        sid = mgr.new()
        live = mgr.live(sid)
        self.assertEqual(live.loop._broker.policy.protected_dirs, [])   # type: ignore[attr-defined]
        self.assertEqual(mgr.active_plugin_tools(), [])


class ChannelFlags(unittest.TestCase):
    def test_ask_carries_plugin_flags(self):
        import queue as q

        from serve.harness.loop import ApprovalRequest
        from serve.harness.manager import ApprovalChannel
        ch = ApprovalChannel(q.Queue())
        ch.plugin_tools = {"plugin_echo"}
        ch.protected_dirs = [Path("/home/x/plugins")]
        req = ApprovalRequest(call_id="c1", tool="plugin_echo", args={})
        ask = {"call_id": req.call_id, "tool": req.tool, "args": req.args, "preview": req.args_preview}
        if req.tool in ch.plugin_tools:
            ask["plugin"] = True
        if targets_protected(req.tool, req.args, ch.protected_dirs):
            ask["plugin_write"] = True
        with ch._lock:
            ch._asks["c1"] = ask
        pending = ch.pending()[0]
        self.assertTrue(pending["plugin"])
        self.assertNotIn("plugin_write", pending)     # plugin tools are not writes
        req2 = ApprovalRequest(call_id="c2", tool="write_file",
                               args={"path": "/home/x/plugins/p.py", "content": "x"})
        self.assertTrue(targets_protected(req2.tool, req2.args, ch.protected_dirs))


if __name__ == "__main__":
    unittest.main()
