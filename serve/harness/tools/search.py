"""serve/harness/tools/search.py — content and filename search (pure stdlib).

rg is used when present (fast, respects .gitignore); otherwise a bounded
os.walk regex scan. Read-only, ungated.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from ..loop import ToolDefinition

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "target", "dist", "build"}
MAX_RESULTS = 200


def search_tool(root: str | Path | None = None) -> ToolDefinition:
    def execute(args: dict, ctx) -> dict:
        pattern = str(args["pattern"])
        base = Path(args.get("path") or root or Path.cwd()).resolve()
        glob_filter = args.get("file_glob")
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return {"text": f"bad regex: {exc}", "is_error": True}
        if shutil.which("rg"):
            cmd = ["rg", "--line-number", "--no-heading", "--color", "never", "--max-count", str(MAX_RESULTS)]
            if glob_filter:
                cmd += ["--glob", glob_filter]
            cmd += ["--", pattern, str(base)]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            except subprocess.TimeoutExpired:
                return {"text": "search timed out", "is_error": True}
            lines = proc.stdout.splitlines()[:MAX_RESULTS]
            if not lines:
                return {"text": "no matches"}
            extra = "\n[more matches truncated]" if len(proc.stdout.splitlines()) > MAX_RESULTS else ""
            return {"text": "\n".join(lines) + extra}
        # stdlib fallback
        hits: list[str] = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                if glob_filter and not Path(fn).match(glob_filter):
                    continue
                fp = Path(dirpath) / fn
                try:
                    if fp.stat().st_size > 2_000_000:
                        continue
                    with open(fp, encoding="utf-8", errors="strict") as f:
                        for i, line in enumerate(f, 1):
                            if rx.search(line):
                                hits.append(f"{fp}:{i}: {line.rstrip()}")
                                if len(hits) >= MAX_RESULTS:
                                    break
                except (OSError, UnicodeDecodeError):
                    continue
            if len(hits) >= MAX_RESULTS:
                break
        return {"text": "\n".join(hits) if hits else "no matches"}

    return ToolDefinition(
        name="search",
        description="Regex search inside files (rg when available). Returns path:line: text. "
                    "file_glob filters by name, e.g. '*.py'.",
        parameters={"type": "object", "properties": {
            "pattern": {"type": "string"}, "path": {"type": "string"}, "file_glob": {"type": "string"}},
            "required": ["pattern"]},
        execute=execute, concurrency_safe=True)
