"""serve/harness/skills.py — the skills store (ADR-0018, M11c).

One directory per skill under <harness>/skills/, each with a SKILL.md:
YAML frontmatter (name, description) + a markdown body. Progressive
disclosure: the system prompt carries only the index (name: one-liner
per visible skill); the model pulls bodies through skill_view. Skills
are prompt content — nothing here executes (contrast ADR-0014).

The store lives OUTSIDE the session fence (ADR-0018 §1): the agent
reaches it only through the two tools below, path-locked to the store.
"""
from __future__ import annotations

import re
from pathlib import Path

from .loop import ToolDefinition

MAX_SKILL_BYTES = 32 * 1024          # SKILL.md cap (ADR-0018 §3)
MAX_VIEW_BYTES = 64 * 1024           # supporting-file read cap
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _parse_skill(path: Path) -> tuple[str, str, str] | str:
    """-> (name, description, body) or an error string (fail closed per skill)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"unreadable: {exc.strerror or exc}"
    if len(raw.encode("utf-8")) > MAX_SKILL_BYTES:
        return f"too large ({len(raw.encode('utf-8'))} bytes, cap {MAX_SKILL_BYTES})"
    if not raw.startswith("---"):
        return "missing YAML frontmatter"
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return "unterminated frontmatter"
    try:
        import yaml
        doc = yaml.safe_load(parts[1]) or {}
    except Exception as exc:  # noqa: BLE001
        return f"bad frontmatter: {exc}"
    if not isinstance(doc, dict):
        return "frontmatter must be a mapping"
    name, desc = doc.get("name"), doc.get("description")
    if not isinstance(name, str) or not NAME_RE.match(name):
        return "frontmatter 'name' missing or not [a-z0-9][a-z0-9_-]{0,63}"
    if not isinstance(desc, str) or not desc.strip():
        return "frontmatter 'description' missing"
    return name, desc.strip(), parts[2].lstrip("\n")


def scan_skills(skills_dir: Path) -> tuple[list[dict], list[str]]:
    """-> ([{name, description, dir}], [warnings]). Unknown dirs are skipped."""
    out: list[dict] = []
    warns: list[str] = []
    if not skills_dir.is_dir():
        return out, warns
    for entry in sorted(skills_dir.iterdir()):
        if not entry.is_dir():
            continue
        parsed = _parse_skill(entry / "SKILL.md")
        if isinstance(parsed, str):
            warns.append(f"skill {entry.name!r} skipped: {parsed}")
            continue
        name, desc, _body = parsed
        if name != entry.name:
            warns.append(f"skill {entry.name!r} skipped: frontmatter name {name!r} != dir name")
            continue
        out.append({"name": name, "description": desc, "dir": entry})
    return out, warns


def index_section(skills: list[dict], warnings: list[str]) -> str | None:
    """The per-turn system section: one line per skill (ADR-0018 §2)."""
    if not skills and not warnings:
        return None
    lines = ["## Available skills",
             "Pull a skill's full body with skill_view(name) when one matches the task at hand."]
    lines += [f"- {s['name']}: {s['description']}" for s in skills]
    lines += [f"(warning: {w})" for w in warnings]
    return "\n".join(lines)


def _safe_file(skill_dir: Path, file_path: str) -> Path | None:
    """A supporting file inside the skill's own dir — no traversal out."""
    if not file_path or Path(file_path).is_absolute():
        return None
    p = (skill_dir / file_path).resolve()
    root = skill_dir.resolve()
    if root != p.parent and root not in p.parents:
        return None
    return p


def skill_view_tool(skills_dir: Path, visible: set[str] | None):
    """visible=None: every skill; a set: only those names (preset gate)."""
    def execute(args: dict, ctx) -> dict:
        name = str(args.get("name", ""))
        if visible is not None and name not in visible:
            return {"text": f"skill {name!r} is not available in this session", "is_error": True}
        parsed = _parse_skill(skills_dir / name / "SKILL.md")
        if isinstance(parsed, str):
            return {"text": f"skill {name!r}: {parsed}", "is_error": True}
        _n, _d, body = parsed
        fp = args.get("file_path")
        if fp:
            p = _safe_file(skills_dir / name, str(fp))
            if p is None:
                return {"text": f"file_path must be a relative path inside skill {name!r}", "is_error": True}
            if not p.is_file():
                return {"text": f"skill {name!r} has no file {str(fp)!r}", "is_error": True}
            try:
                data = p.read_bytes()
            except OSError as exc:
                return {"text": f"skill {name!r}/{fp}: unreadable ({exc.strerror or exc})", "is_error": True}
            if len(data) > MAX_VIEW_BYTES:
                data = data[:MAX_VIEW_BYTES] + b"\n[truncated]"
            return {"text": f"# skill {name} · {fp}\n{data.decode('utf-8', 'replace')}"}
        return {"text": f"# skill {name}\n{body}"}

    return _tool("skill_view",
                 "Read a skill's full body (SKILL.md) or one of its supporting files "
                 "(file_path, relative to the skill dir). Names come from the Available skills index.",
                 {"type": "object", "properties": {
                     "name": {"type": "string"}, "file_path": {"type": "string"}}},
                 execute)


def skill_write_tool(skills_dir: Path):
    def execute(args: dict, ctx) -> dict:
        name = str(args.get("name", ""))
        content = args.get("content")
        if not NAME_RE.match(name):
            return {"text": "name must match [a-z0-9][a-z0-9_-]{0,63}", "is_error": True}
        if not isinstance(content, str) or not content.strip():
            return {"text": "content (full SKILL.md text) is required", "is_error": True}
        if len(content.encode("utf-8")) > MAX_SKILL_BYTES:
            return {"text": f"content too large (cap {MAX_SKILL_BYTES} bytes)", "is_error": True}
        parsed = _parse_skill_from_text(content)
        if isinstance(parsed, str):
            return {"text": f"refused: {parsed}", "is_error": True}
        front_name, _desc, _body = parsed
        if front_name != name:
            return {"text": f"frontmatter name {front_name!r} must equal the skill name {name!r}", "is_error": True}
        target = skills_dir / name
        action = "updated" if (target / "SKILL.md").exists() else "created"
        try:
            target.mkdir(parents=True, exist_ok=True)
            (target / "SKILL.md").write_text(content, encoding="utf-8")
        except OSError as exc:
            return {"text": f"write failed: {exc.strerror or exc}", "is_error": True}
        return {"text": f"skill {name!r} {action}. It appears in the index from the next turn."}

    return _tool("skill_write",
                 "Create or replace a skill's SKILL.md in the store (frontmatter name+description "
                 "required; the name must match the frontmatter). Prompt content only — it executes nothing.",
                 {"type": "object", "properties": {
                     "name": {"type": "string"}, "content": {"type": "string"}}},
                 execute)


def _parse_skill_from_text(text: str) -> tuple[str, str, str] | str:
    """Validate SKILL.md text without touching disk (same rules as _parse_skill)."""
    if not text.startswith("---"):
        return "missing YAML frontmatter"
    parts = text.split("---", 2)
    if len(parts) < 3:
        return "unterminated frontmatter"
    try:
        import yaml
        doc = yaml.safe_load(parts[1]) or {}
    except Exception as exc:  # noqa: BLE001
        return f"bad frontmatter: {exc}"
    if not isinstance(doc, dict):
        return "frontmatter must be a mapping"
    name, desc = doc.get("name"), doc.get("description")
    if not isinstance(name, str) or not NAME_RE.match(name):
        return "frontmatter 'name' missing or not [a-z0-9][a-z0-9_-]{0,63}"
    if not isinstance(desc, str) or not desc.strip():
        return "frontmatter 'description' missing"
    return name, desc.strip(), parts[2].lstrip("\n")


def _tool(name: str, description: str, parameters: dict, execute) -> ToolDefinition:
    return ToolDefinition(name=name, description=description, parameters=parameters,
                          execute=execute, concurrency_safe=True)
