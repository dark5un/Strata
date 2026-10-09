"""serve/harness/presets.py — agent presets: YAML persona + tool restrictions (ADR-0009).

A preset is data, not code: serve/harness/presets/<name>.yaml with

    name: researcher
    persona: "You are a focused research subagent..."   # system section only
    tools:
      allow: [read_file, search]                        # optional whitelist
      deny: [shell, write_file, edit_file]              # optional blacklist

The built-in "default" preset is today's behaviour: no persona, no
restrictions. Loading fails closed: a missing/unknown/malformed preset
attaches as "default" with a warning, never with an arbitrary tool set.

Tool restrictions intersect over the ancestor chain (dsh's rule, adopted in
ADR-0009): effective(registry, preset, parent_effective) — a child can never
gain a tool its parent did not have.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

PRESET_DIR = Path(__file__).resolve().parent / "presets"


@dataclass
class Preset:
    name: str
    persona: str | None = None
    allow: list[str] = field(default_factory=list)   # empty = all
    deny: list[str] = field(default_factory=list)

    def permits(self, tool_name: str) -> bool:
        if self.allow and tool_name not in self.allow:
            return False
        return tool_name not in self.deny

    def section(self) -> str | None:
        """The persona rides system_sections (ADR-0007): prompt-only."""
        return self.persona or None


DEFAULT_PRESET = Preset(name="default")


def load_preset(name: str, presets_dir: Path | None = None) -> tuple[Preset, str | None]:
    """Resolve one preset. Returns (preset, warning); unknown/malformed -> default + warning."""
    if not name or name == "default":
        return DEFAULT_PRESET, None
    directory = presets_dir or PRESET_DIR
    path = directory / f"{name}.yaml"
    if not path.exists():
        path = directory / f"{name}.yml"
    if not path.exists():
        return DEFAULT_PRESET, f"unknown preset {name!r}: using default"
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
        return Preset(name=str(doc.get("name") or name), persona=persona, allow=allow, deny=deny), None
    except Exception as exc:  # noqa: BLE001 - fail closed
        return DEFAULT_PRESET, f"preset {name!r} is malformed ({exc}): using default"


def list_presets(presets_dir: Path | None = None) -> list[dict]:
    """Names + personas for the UI's preset select. Unreadable files are skipped."""
    directory = presets_dir or PRESET_DIR
    out = [{"name": "default", "persona": None}]
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.y*ml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(doc, dict):
                out.append({"name": str(doc.get("name") or path.stem),
                            "persona": (doc.get("persona") or None) if isinstance(doc.get("persona"), str) else None})
        except Exception:  # noqa: BLE001
            continue
    return out
