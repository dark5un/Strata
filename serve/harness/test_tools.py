"""serve/harness/test_tools.py — the M1 tool set against real temp files.

    python -m unittest serve.harness.test_tools -v
"""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from .session import create
from .tools import default_registry
from .tools.ask import QuestionBoard
from .tools.fs import _resolve, edit_tool, read_tool, write_tool
from .tools.search import search_tool
from .tools.shell import shell_tool
from .tools.todo import fold_todo, todo_tool


def ctx_for(session):
    from .loop import ToolRunContext
    return ToolRunContext(session=session, cancel=threading.Event(), conclude_turn=lambda: None)


class FsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.s = create(self.root / "sessions")
        self.ctx = ctx_for(self.s)

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_read_edit_roundtrip(self):
        f = self.root / "sub" / "a.txt"
        r = write_tool(self.root).execute({"path": str(f), "content": "hello world"}, self.ctx)
        self.assertFalse(r.get("is_error"))
        r = read_tool().execute({"path": str(f)}, self.ctx)
        self.assertIn("1|hello world", r["text"])
        r = edit_tool(self.root).execute({"path": str(f), "old_string": "world", "new_string": "there"}, self.ctx)
        self.assertFalse(r.get("is_error"))
        self.assertEqual(f.read_text(), "hello there")

    def test_edit_ambiguous_rejected(self):
        f = self.root / "b.txt"
        f.write_text("x x x")
        r = edit_tool(self.root).execute({"path": str(f), "old_string": "x", "new_string": "y"}, self.ctx)
        self.assertTrue(r["is_error"])
        r = edit_tool(self.root).execute({"path": str(f), "old_string": "x", "new_string": "y",
                                          "replace_all": True}, self.ctx)
        self.assertFalse(r.get("is_error"))
        self.assertEqual(f.read_text(), "y y y")

    def test_root_fence_blocks_escape(self):
        with self.assertRaises(ValueError):
            _resolve("../../etc/passwd", self.root)
        r = write_tool(self.root).execute({"path": "../escape.txt", "content": "x"}, self.ctx)
        self.assertTrue(r["is_error"])
        self.assertIn("escapes the trusted roots", r["text"])

    def test_read_pagination(self):
        f = self.root / "c.txt"
        f.write_text("\n".join(f"line{i}" for i in range(1, 51)))
        r = read_tool().execute({"path": str(f), "offset": 41, "limit": 10}, self.ctx)
        self.assertIn("41|line41", r["text"])
        self.assertNotIn("line40", r["text"])


class ShellTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.s = create(Path(self._tmp.name) / "sessions")
        self.ctx = ctx_for(self.s)

    def tearDown(self):
        self._tmp.cleanup()

    def test_echo_and_exit_code(self):
        r = shell_tool(cwd=self._tmp.name).execute({"command": "echo hi"}, self.ctx)
        self.assertIn("exit 0", r["text"])
        self.assertIn("hi", r["text"])
        r = shell_tool(cwd=self._tmp.name).execute({"command": "false"}, self.ctx)
        self.assertTrue(r["is_error"])
        self.assertEqual(r["meta"]["exit_code"], 1)

    def test_timeout(self):
        r = shell_tool(timeout_s=0.3, cwd=self._tmp.name).execute({"command": "sleep 5"}, self.ctx)
        self.assertTrue(r["is_error"])
        self.assertIn("timed out", r["text"])

    def test_output_capped(self):
        r = shell_tool(cwd=self._tmp.name).execute({"command": "yes x | head -c 100000"}, self.ctx)
        self.assertIn("truncated", r["text"])


class TrustedRootsTests(unittest.TestCase):
    """M9 (ADR-0015): per-session trusted workspaces — the multi-root fence."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.a = base / "a"
        self.b = base / "b"
        (self.a / "sub").mkdir(parents=True)
        self.b.mkdir()
        self.outside = base / "outside"
        self.outside.mkdir()
        self.s = create(base / "sessions")
        self.ctx = ctx_for(self.s)
        self.roots = [self.a, self.b]

    def tearDown(self):
        self._tmp.cleanup()

    def test_multi_root_accepts_either(self):
        for r in (write_tool(self.roots).execute({"path": str(self.a / "x.txt"), "content": "1"}, self.ctx),
                  write_tool(self.roots).execute({"path": str(self.b / "y.txt"), "content": "2"}, self.ctx),
                  write_tool(self.roots).execute({"path": str(self.a / "sub" / "z.txt"), "content": "3"}, self.ctx)):
            self.assertFalse(r.get("is_error"), r["text"])

    def test_escape_names_all_roots(self):
        r = write_tool(self.roots).execute({"path": str(self.outside / "no.txt"), "content": "x"}, self.ctx)
        self.assertTrue(r["is_error"])
        self.assertIn("escapes the trusted roots", r["text"])
        self.assertIn(str(self.a), r["text"])          # the agent learns the boundary
        self.assertIn(str(self.b), r["text"])

    def test_symlink_escape_blocked(self):
        link = self.a / "link"
        link.symlink_to(self.outside)
        r = write_tool(self.roots).execute({"path": str(link / "evil.txt"), "content": "x"}, self.ctx)
        self.assertTrue(r["is_error"])                  # resolved before the check
        self.assertFalse((self.outside / "evil.txt").exists())

    def test_read_fenced_too(self):
        (self.outside / "secret.txt").write_text("s")
        r = read_tool(self.roots).execute({"path": str(self.outside / "secret.txt")}, self.ctx)
        self.assertTrue(r["is_error"])
        self.assertIn("escapes the trusted roots", r["text"])

    def test_relative_paths_resolve_against_first_root(self):
        r = write_tool(self.roots).execute({"path": "rel.txt", "content": "x"}, self.ctx)
        self.assertFalse(r.get("is_error"), r["text"])
        self.assertTrue((self.a / "rel.txt").exists())

    def test_shell_cwd_fenced(self):
        r = shell_tool(cwd=self.a, roots=self.roots).execute(
            {"command": "pwd"}, self.ctx)
        self.assertIn(str(self.a), r["text"])           # default: first root
        r = shell_tool(cwd=self.a, roots=self.roots).execute(
            {"command": "pwd", "cwd": str(self.b)}, self.ctx)
        self.assertIn(str(self.b), r["text"])           # inside another root: allowed
        r = shell_tool(cwd=self.a, roots=self.roots).execute(
            {"command": "pwd", "cwd": str(self.outside)}, self.ctx)
        self.assertTrue(r["is_error"])
        self.assertIn("escapes the trusted roots", r["text"])

    def test_search_path_fenced(self):
        r = search_tool(self.roots).execute({"pattern": "needle", "path": str(self.outside)}, self.ctx)
        self.assertTrue(r["is_error"])
        self.assertIn("escapes the trusted roots", r["text"])


class SearchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "a.py").write_text("needle here\nsecond needle\n")
        (self.root / "b.txt").write_text("needle too\n")
        self.s = create(self.root / "sessions")
        self.ctx = ctx_for(self.s)

    def tearDown(self):
        self._tmp.cleanup()

    def test_regex_search(self):
        r = search_tool(self.root).execute({"pattern": "needle", "file_glob": "*.py"}, self.ctx)
        self.assertIn("a.py:1:", r["text"])
        self.assertNotIn("b.txt", r["text"])

    def test_bad_regex_is_error(self):
        r = search_tool(self.root).execute({"pattern": "(unbalanced"}, self.ctx)
        self.assertTrue(r["is_error"])


class AskTests(unittest.TestCase):
    def test_answer_flow(self):
        board = QuestionBoard()
        tool = __import__("serve.harness.tools.ask", fromlist=["ask_tool"]).ask_tool(board)
        s = create(Path(tempfile.mkdtemp()))
        ctx = ctx_for(s)
        t = threading.Thread(target=lambda: tool.execute(
            {"questions": [{"question": "pick?", "options": [{"label": "a"}]}], "timeout_s": 5}, ctx))
        t.start()
        while not board.pending():
            threading.Event().wait(0.01)
        q = board.pending()[0]
        self.assertTrue(board.answer(q.qid, {"selected": ["a"]}))
        t.join(6)
        self.assertFalse(t.is_alive())

    def test_timeout_releases_model(self):
        board = QuestionBoard()
        from .tools.ask import ask_tool
        tool = ask_tool(board)
        s = create(Path(tempfile.mkdtemp()))
        r = tool.execute({"questions": [{"question": "still there?"}], "timeout_s": 0.1}, ctx_for(s))
        self.assertFalse(r.get("is_error"))
        self.assertTrue(r["meta"]["timed_out"])
        self.assertEqual(len(board.pending()), 1)              # question stays answerable

    def test_late_answer_after_timeout(self):
        board = QuestionBoard()
        from .tools.ask import ask_tool
        tool = ask_tool(board)
        s = create(Path(tempfile.mkdtemp()))
        r = tool.execute({"questions": [{"question": "q"}], "timeout_s": 0.05}, ctx_for(s))
        q = board.pending()[0]
        board.answer(q.qid, {"text": "late"})
        self.assertIn("still waiting", r["text"])


class TodoTests(unittest.TestCase):
    def test_set_read_fold(self):
        s = create(Path(tempfile.mkdtemp()))
        ctx = ctx_for(s)
        r = todo_tool().execute({"items": [{"content": "a", "status": "pending"},
                                           {"content": "b", "status": "in_progress"}]}, ctx)
        self.assertIn("[>]", r["text"])
        r = todo_tool().execute({}, ctx)
        self.assertIn("[ ] a", r["text"])
        self.assertEqual(len(fold_todo(s.events)), 2)

    def test_bad_status_rejected(self):
        s = create(Path(tempfile.mkdtemp()))
        r = todo_tool().execute({"items": [{"content": "a", "status": "wat"}]}, ctx_for(s))
        self.assertTrue(r["is_error"])


class RegistryTests(unittest.TestCase):
    def test_default_registry_gating(self):
        reg = default_registry(board=QuestionBoard(), root=tempfile.mkdtemp())
        self.assertEqual(set(reg.names()),
                         {"read_file", "write_file", "edit_file", "search", "shell", "todo", "ask_user"})
        self.assertTrue(reg.needs_approval("shell"))
        self.assertTrue(reg.needs_approval("write_file"))
        self.assertFalse(reg.needs_approval("read_file"))
        self.assertFalse(reg.needs_approval("search"))

    def test_duplicate_name_rejected(self):
        reg = default_registry(root=tempfile.mkdtemp())
        from .loop import ToolDefinition
        with self.assertRaises(ValueError):
            reg.register(ToolDefinition(name="shell", description="", parameters={}, execute=lambda a, c: {}))


if __name__ == "__main__":
    unittest.main()
