"""serve/harness/presets.py — agent presets: YAML persona + tool restrictions (ADR-0009, ADR-0011).

A preset is data, not code: presets/<name>.yaml with

    name: researcher
    persona: "You are a focused research subagent..."   # system section only
    tools:
      allow: [read_file, search]                        # optional whitelist
      deny: [shell, write_file, edit_file]              # optional blacklist

Two directories (ADR-0011): the USER dir (~/.local/share/strata/harness/presets,
overridable via STRATA_HARNESS_PRESET_DIR) wins on name collision; the bundled
dir (serve/harness/presets/) is read-only by contract — the UI edits copy into
the user dir. The built-in "default" preset lives in code: today's behaviour,
no persona, no restrictions. Loading fails closed: a missing/unknown/malformed
preset attaches as "default" with a warning, never with an arbitrary tool set.

Tool restrictions intersect over the ancestor chain (dsh's rule, adopted in
ADR-0009): effective(registry, preset, parent_effective) — a child can never
gain a tool its parent did not have.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PRESET_DIR = Path(__file__).resolve().parent / "presets"
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def user_preset_dir() -> Path:
    return Path(os.environ.get("STRATA_HARNESS_PRESET_DIR",
                               Path.home() / ".local/share/strata/harness/presets"))


@dataclass
class Preset:
    name: str
    persona: str | None = None
    allow: list[str] = field(default_factory=list)   # empty = all
    deny: list[str] = field(default_factory=list)
    description: str | None = None

    def permits(self, tool_name: str) -> bool:
        if self.allow and tool_name not in self.allow:
            return False
        return tool_name not in self.deny

    def section(self) -> str | None:
        """The persona rides system_sections (ADR-0007): prompt-only."""
        return self.persona or None


DEFAULT_PRESET = Preset(name="default")


def _parse(path: Path) -> tuple[Preset, str | None]:
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(doc, dict):
            raise ValueError("preset YAML must be a mapping")
        tools = doc.get("tools") or {}
        if not isinstance(tools, dict):
            raise ValueError("preset 'tools' must be a mapping")
        allow = [str(t) for t in (tools.get("allow") or [])]
        deny = [str(t) for t in (tools.get("deny") or [])]
        persona = doc.get("persona")
        if persona is not None and not isinstance(persona, str):
            raise ValueError("preset 'persona' must be a string")
        description = doc.get("description")
        if description is not None and not isinstance(description, str):
            raise ValueError("preset 'description' must be a string")
        return Preset(name=str(doc.get("name") or path.stem), persona=persona,
                      allow=allow, deny=deny, description=description), None
    except Exception as exc:  # noqa: BLE001 - fail closed
        return DEFAULT_PRESET, f"preset {path.stem!r} is malformed ({exc}): using default"


def _dirs(presets_dir: Path | None) -> list[tuple[Path, str]]:
    """Resolution order (ADR-0011): explicit dir, else user dir then bundled."""
    if presets_dir is not None:
        return [(presets_dir, "user")]
    return [(user_preset_dir(), "user"), (PRESET_DIR, "bundled")]


def find_preset_file(name: str, presets_dir: Path | None = None) -> tuple[Path | None, str | None]:
    """Where a preset lives: (path, source) or (None, None). User dir wins."""
    if not name or name == "default":
        return None, None
    for directory, source in _dirs(presets_dir):
        for ext in (".yaml", ".yml"):
            path = directory / f"{name}{ext}"
            if path.exists():
                return path, source
    return None, None


def load_preset(name: str, presets_dir: Path | None = None) -> tuple[Preset, str | None]:
    """Resolve one preset. Returns (preset, warning); unknown/malformed -> default + warning."""
    if not name or name == "default":
        return DEFAULT_PRESET, None
    path, _source = find_preset_file(name, presets_dir)
    if path is None:
        return DEFAULT_PRESET, f"unknown preset {name!r}: using default"
    return _parse(path)


def list_presets(presets_dir: Path | None = None) -> list[dict]:
    """Names + personas + source badge for the UI's preset select and manage
    panel. User entries shadow bundled ones of the same name (ADR-0011);
    unreadable files are skipped."""
    out: dict[str, dict] = {"default": {"name": "default", "persona": None, "source": "bundled"}}
    for directory, source in _dirs(presets_dir):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.y*ml")):
            preset, warning = _parse(path)
            if warning:
                continue
            entry = {"name": preset.name, "persona": preset.persona,
                     "description": preset.description, "source": source}
            if source == "user" or entry["name"] not in out:
                out[entry["name"]] = entry
    return sorted(out.values(), key=lambda p: p["name"])


def _write_dir(presets_dir: Path | None) -> Path:
    """Where CRUD writes: the explicit dir (tests), else the user dir (ADR-0011)."""
    return presets_dir if presets_dir is not None else user_preset_dir()


def _check_name(name: str) -> str:
    """Preset names: [A-Za-z0-9._-], must contain an alphanumeric (so '.', '..'
    and '...' are out — they'd collide with dotfiles and confuse the loader)."""
    if not NAME_RE.match(name or "") or not re.search(r"[A-Za-z0-9]", name):
        raise ValueError("preset name: 1-64 chars of [A-Za-z0-9._-], with at least one letter or digit")
    return name


def save_preset(presets_dir: Path | None, name: str, *, persona: str | None,
                allow: list[str], deny: list[str], description: str | None = None) -> Path:
    """Write a preset into the USER dir (bundled files stay read-only, ADR-0011)."""
    _check_name(name)
    if name == "default":
        raise ValueError("preset name: 1-64 chars of [A-Za-z0-9._-], never 'default'")
    directory = _write_dir(presets_dir)
    directory.mkdir(parents=True, exist_ok=True)
    doc: dict = {"name": name}
    if description:
        doc["description"] = description
    if persona:
        doc["persona"] = persona
    if allow or deny:
        doc["tools"] = {}
        if allow:
            doc["tools"]["allow"] = list(allow)
        if deny:
            doc["tools"]["deny"] = list(deny)
    path = directory / f"{name}.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def delete_preset(presets_dir: Path | None, name: str) -> bool:
    """Delete from the user dir only; a bundled preset is never removable (ADR-0011)."""
    _check_name(name)
    if name == "default":
        raise ValueError("preset name: 1-64 chars of [A-Za-z0-9._-], never 'default'")
    directory = _write_dir(presets_dir)
    removed = False
    for ext in (".yaml", ".yml"):
        path = directory / f"{name}{ext}"
        if path.exists():
            path.unlink()
            removed = True
    return removed
