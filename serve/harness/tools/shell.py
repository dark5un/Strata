"""serve/harness/tools/shell.py — the shell tool (approval-gated).

subprocess with a hard timeout; output capped; the cancel event from the loop
kills the child. cwd defaults to the session's workspace root.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from ..loop import Cancelled, ToolDefinition

MAX_OUTPUT_CHARS = 50_000


def shell_tool(timeout_s: float = 120.0, cwd: str | Path | None = None) -> ToolDefinition:
    def execute(args: dict, ctx) -> dict:
        command = str(args["command"])
        workdir = str(args.get("cwd") or cwd or Path.cwd())
        try:
            proc = subprocess.run(
                ["bash", "-c", command], cwd=workdir, capture_output=True, text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            return {"text": f"timed out after {timeout_s}s: {command}", "is_error": True}
        except Exception as exc:  # noqa: BLE001
            return {"text": f"shell failed: {exc}", "is_error": True}
        if ctx.cancel.is_set():
            raise Cancelled
        out = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
        if len(out) > MAX_OUTPUT_CHARS:
            out = out[:MAX_OUTPUT_CHARS] + f"\n[truncated at {MAX_OUTPUT_CHARS} chars]"
        return {"text": f"exit {proc.returncode}\n{out}", "is_error": proc.returncode != 0,
                "meta": {"exit_code": proc.returncode}}

    return ToolDefinition(
        name="shell",
        description="Run a bash command in the workspace and return its output and exit code.",
        parameters={"type": "object", "properties": {"command": {"type": "string"}, "cwd": {"type": "string"}},
                    "required": ["command"]},
        execute=execute, concurrency_safe=False, timeout_s=timeout_s + 10)
