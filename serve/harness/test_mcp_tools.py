"""serve/harness/test_mcp_tools.py — MCP hub tools in the harness registry (ADR-0012).

The real McpHub against serve/mcp_fake_server.py (stdio, no GPU, no SDK):
the tools arrive namespaced and gated, a call routes through hub.call with
the loop's cancel event, and the manager attaches them into a live session.

    python -m unittest serve.harness.test_mcp_tools -v
"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

from serve.mcp import McpHub

from . import events as ev  # noqa: E402
from .engine import EngineResult  # noqa: E402
from .loop import ToolDefinition  # noqa: E402
from .manager import HarnessManager  # noqa: E402
from .tools.mcp import mcp_tools  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
FAKE = str(ROOT / "serve" / "mcp_fake_server.py")


def stdio_hub() -> McpHub:
    hub = McpHub({"fake": {"command": sys.executable, "args": [FAKE]}})
    hub.start(wait=True)
    return hub


def scripted(script):
    box = {"i": 0}

    def factory():
        def engine(messages, tools, on_event=None):
            i = box["i"]
            box["i"] += 1
            if i >= len(script):
                return EngineResult(content="done", usage={"prompt_tokens": 1, "completion_tokens": 1})
            return script[i]
        return engine
    return factory


def wait_idle(mgr: HarnessManager, sid: str, timeout: float = 10.0) -> None:
    import time
    end = time.time() + timeout
    while time.time() < end:
        if not mgr.live(sid).busy:
            return
        time.sleep(0.02)
    raise TimeoutError(f"session {sid} still busy")


class McpToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hub = stdio_hub()

    @classmethod
    def tearDownClass(cls):
        cls.hub.close()

    def test_tools_are_namespaced_and_gated(self):
        tools = mcp_tools(self.hub)
        names = {t.name for t in tools}
        self.assertIn("fake__echo", names)
        self.assertIn("fake__add", names)
        for t in tools:
            self.assertTrue(t.needs_approval)          # ADR-0012: every hub tool is gated
            self.assertIsInstance(t, ToolDefinition)
        echo = next(t for t in tools if t.name == "fake__echo")
        self.assertIn("text", echo.parameters.get("properties", {}))

    def test_call_routes_through_the_hub(self):
        echo = next(t for t in mcp_tools(self.hub) if t.name == "fake__echo")
        ctx = type("Ctx", (), {"cancel": threading.Event()})()
        r = echo.execute({"text": "hi"}, ctx)
        self.assertFalse(r["is_error"])
        self.assertIn("hi", r["text"])

    def test_error_result_is_text_not_exception(self):
        fail = next(t for t in mcp_tools(self.hub) if t.name == "fake__fail")
        ctx = type("Ctx", (), {"cancel": threading.Event()})()
        r = fail.execute({}, ctx)
        self.assertTrue(r["is_error"])
        self.assertTrue(r["text"].startswith("error:"))


class McpSessionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.hub = stdio_hub()

    def tearDown(self):
        self.hub.close()
        self._tmp.cleanup()

    def test_session_gets_mcp_tools_and_approval_shows_full_args(self):
        script = [
            EngineResult(tool_calls=[{"id": "c1", "name": "fake__add",
                                      "arguments": {"a": 2, "b": 3}}],
                         usage={"prompt_tokens": 3, "completion_tokens": 2}),
            EngineResult(content="five", usage={"prompt_tokens": 4, "completion_tokens": 1}),
        ]
        mgr = HarnessManager(self.root / "sessions", scripted(script),
                             spill_dir=self.root / "spill", workspace_root=self.root,
                             mcp_hub=self.hub)
        sid = mgr.new()
        self.assertIn("fake__add", mgr.live(sid).loop.tools)   # attached into the registry
        mgr.send(sid, "add two and three")
        live = mgr.live(sid)
        ask = live.approvals.get(timeout=5)
        self.assertEqual(ask["tool"], "fake__add")
        self.assertEqual(ask["args"], {"a": 2, "b": 3})        # full payload on the card (M7a rule)
        mgr.decide(sid, "c1", "allow-once")
        wait_idle(mgr, sid)
        results = [r for r in live.handle.events if r["type"] == ev.TOOL_RESULT]
        self.assertIn("5", results[0]["data"]["text"])

    def test_preset_deny_removes_mcp_tool(self):
        # ADR-0009 vocabulary covers hub names: a deny preset drops them like any tool
        mgr = HarnessManager(self.root / "sessions", scripted([]),
                             spill_dir=self.root / "spill", workspace_root=self.root,
                             mcp_hub=self.hub, presets_dir=self.root / "presets")
        (self.root / "presets").mkdir()
        (self.root / "presets" / "no-mcp.yaml").write_text(
            "name: no-mcp\ntools:\n  deny: [fake__echo, fake__add, fake__fail, fake__sleep, fake__big, fake__die]\n",
            encoding="utf-8")
        sid = mgr.new(preset="no-mcp")
        self.assertNotIn("fake__echo", mgr.live(sid).loop.tools)
        self.assertIn("shell", mgr.live(sid).loop.tools)


if __name__ == "__main__":
    unittest.main()
