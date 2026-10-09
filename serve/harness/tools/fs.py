"""serve/harness/tools/fs.py — read / write / edit file tools.

read is ungated; write and edit are approval-gated by the registry. When the
registry passes root=, every path must resolve inside it (the harness's
workspace fence); with root=None the operator accepted the full-RCE surface
(same posture as dsh's loopback fence — the server binds 127.0.0.1).
"""
from __future__ import annotations

from pathlib import Path

from ..loop import ToolDefinition

MAX_READ_LINES = 2000
MAX_READ_CHARS = 100_000


def _resolve(path: str, root: Path | None) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = (root or Path.cwd()) / p
    p = p.resolve()
    if root is not None:
        root = root.resolve()
        if root != p and root not in p.parents:
            raise ValueError(f"path {path!r} escapes the workspace root {root}")
    return p


def read_tool(root: str | Path | None = None) -> ToolDefinition:
    def execute(args: dict, ctx) -> dict:
        try:
            p = Path(str(args["path"])).expanduser()
            if not p.is_absolute():
                p = (Path(root).resolve() if root else Path.cwd()) / p
            path = p.resolve()
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            return {"text": f"read failed: {exc}", "is_error": True}
        lines = text.splitlines()
        offset = max(1, int(args.get("offset") or 1))
        limit = min(int(args.get("limit") or MAX_READ_LINES), MAX_READ_LINES)
        window = lines[offset - 1: offset - 1 + limit]
        body = "\n".join(f"{offset + i}|{ln}" for i, ln in enumerate(window))
        truncated = len(body) > MAX_READ_CHARS
        if truncated:
            body = body[:MAX_READ_CHARS]
        return {"text": body + (f"\n[truncated; {len(lines)} lines total]" if truncated or offset - 1 + limit < len(lines) else "")}

    return ToolDefinition(
        name="read_file",
        description="Read a text file. Returns LINE|content with 1-indexed line numbers. "
                    "Large files: pass offset/limit.",
        parameters={"type": "object", "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "minimum": 1},
            "limit": {"type": "integer", "minimum": 1}},
            "required": ["path"]},
        execute=execute, concurrency_safe=True)


def write_tool(root: str | Path | None = None) -> ToolDefinition:
    def execute(args: dict, ctx) -> dict:
        try:
            path = _resolve(str(args["path"]), Path(root) if root else None)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(args["content"]), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            return {"text": f"write failed: {exc}", "is_error": True}
        return {"text": f"wrote {len(str(args['content']))} chars to {path}"}

    return ToolDefinition(
        name="write_file",
        description="Create or overwrite a text file with the given content.",
        parameters={"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["path", "content"]},
        execute=execute, concurrency_safe=False)


def edit_tool(root: str | Path | None = None) -> ToolDefinition:
    def execute(args: dict, ctx) -> dict:
        try:
            path = _resolve(str(args["path"]), Path(root) if root else None)
            text = path.read_text(encoding="utf-8")
            old, new = str(args["old_string"]), str(args["new_string"])
            replace_all = bool(args.get("replace_all"))
            count = text.count(old)
            if count == 0:
                return {"text": "old_string not found", "is_error": True}
            if count > 1 and not replace_all:
                return {"text": f"old_string matches {count} times; make it unique or set replace_all", "is_error": True}
            path.write_text(text.replace(old, new) if replace_all or count == 1 else text, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            return {"text": f"edit failed: {exc}", "is_error": True}
        return {"text": f"edited {path} ({count if replace_all else 1} replacement(s))"}

    return ToolDefinition(
        name="edit_file",
        description="Replace old_string with new_string in a file. old_string must be unique unless replace_all.",
        parameters={"type": "object", "properties": {
            "path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"},
            "replace_all": {"type": "boolean"}},
            "required": ["path", "old_string", "new_string"]},
        execute=execute, concurrency_safe=False)
