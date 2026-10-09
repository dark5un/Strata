"""serve/harness/tools — the tool registry (ADR-0003).

Every tool is a loop.ToolDefinition. The registry is the single place the
loop and the routes learn which tools exist. Tools that mutate the machine
(shell, write, edit) default to needs_approval=True: the approval policy
decides, fail-closed.
"""
from __future__ import annotations

from ..loop import ToolDefinition  # noqa: F401  (re-export for callers)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition, *, needs_approval: bool = False) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name {tool.name!r}")
        tool.needs_approval = needs_approval  # type: ignore[attr-defined]
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def __iter__(self):
        return iter(self._tools.values())

    def needs_approval(self, name: str) -> bool:
        tool = self._tools.get(name)
        return bool(getattr(tool, "needs_approval", False)) if tool else False


def default_registry(board=None, **opts) -> ToolRegistry:
    """The M1 tool set: fs read/write/edit, search, shell, todo, ask_user.

    board: ask.QuestionBoard for ask_user; when None, ask_user is omitted
    (a headless driver has no one to answer).
    """
    from . import ask as _ask
    from . import fs as _fs
    from . import search as _search
    from . import shell as _shell
    from . import todo as _todo
    reg = ToolRegistry()
    root = opts.get("root")
    entries: list[tuple[ToolDefinition, bool]] = [
        (_fs.read_tool(), False),
        (_search.search_tool(root), False),
        (_todo.todo_tool(), False),
        (_fs.write_tool(root), True),
        (_fs.edit_tool(root), True),
        (_shell.shell_tool(opts.get("shell_timeout_s", 120.0)), True),
    ]
    if board is not None:
        entries.append((_ask.ask_tool(board), False))
    for tool, gated in entries:
        reg.register(tool, needs_approval=gated)
    return reg
