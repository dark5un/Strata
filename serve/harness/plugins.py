"""serve/harness/plugins.py — self-authored plugins: opt-in, hash-gated review (ADR-0014).

A plugin is one .py file in the plugin dir. It is arbitrary Python that runs
with the server's privileges, so two independent gates protect the user:

1. LOAD REVIEW (this module): a plugin is imported ONLY when its sha256
   matches the reviewed manifest (.reviewed.json). New or changed files are
   QUARANTINED — listed, never imported. Reviewing is per (name, hash):
   editing a plugin re-quarantines it. The reviewed source is snapshotted
   under .reviewed/ so the panel can diff a changed file against what was
   last approved.
2. WRITE GATE (approval.py protected dirs): a session's write_file/edit_file
   into the plugin dir always asks, even under yolo/allowlist.

Contract (module-level, all optional):
    name: str            (defaults to the file stem)
    description: str
    tools() -> list[ToolDefinition]
    hooks() -> dict[str, list[Callable]]   # keys: pre_step / pre_tool / post_tool (ADR-0013)

A plugin tool colliding with an existing registry name is a hard error: a
plugin never shadows a built-in. Import/contract errors mark the plugin
"failed" for the attach; the server and other plugins continue.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
import time
import traceback
from pathlib import Path

NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
HOOK_KEYS = ("pre_step", "pre_tool", "post_tool")
MANIFEST = ".reviewed.json"
SNAP_DIR = ".reviewed"


def plugin_dir_default() -> Path:
    return Path.home() / ".local/share/strata/harness/plugins"


def _sha(text: bytes) -> str:
    return hashlib.sha256(text).hexdigest()


def _manifest_path(directory: Path) -> Path:
    return directory / MANIFEST


def load_manifest(directory: Path) -> dict:
    """{name: {"sha256": ..., "reviewed_at": ...}}. A corrupt manifest reads
    as empty (fail closed: nothing is trusted)."""
    try:
        doc = json.loads(_manifest_path(directory).read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            return {}
        return {str(k): v for k, v in doc.items() if isinstance(v, dict) and "sha256" in v}
    except Exception:  # noqa: BLE001
        return {}


def save_manifest(directory: Path, entries: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    _manifest_path(directory).write_text(json.dumps(entries, indent=1, sort_keys=True), encoding="utf-8")


def plugin_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob("*.py")
                  if NAME_RE.match(p.stem) and not p.stem.startswith("_"))


def scan(directory: Path) -> list[dict]:
    """State for every plugin file, WITHOUT importing anything: the panel
    reads this. state: active (hash reviewed) | quarantined | changed
    (reviewed once, hash moved)."""
    manifest = load_manifest(directory)
    out = []
    for path in plugin_files(directory):
        try:
            data = path.read_bytes()
        except OSError as exc:
            out.append({"name": path.stem, "state": "unreadable", "error": str(exc),
                        "sha256": "", "size": 0, "mtime": 0})
            continue
        sha = _sha(data)
        entry = manifest.get(path.stem)
        if entry is None:
            state = "quarantined"
        elif entry["sha256"] == sha:
            state = "active"
        else:
            state = "changed"
        st = path.stat()
        out.append({"name": path.stem, "state": state, "sha256": sha,
                    "size": len(data), "mtime": st.st_mtime,
                    "reviewed_sha": (entry or {}).get("sha256", "")})
    return out


def review(directory: Path, name: str, sha256: str) -> dict:
    """Approve the EXACT bytes the human read: the current file's hash must
    match the sha256 passed in (TOCTOU guard — a file edited between reading
    and approving is refused with ok=False). Snapshots the source under
    SNAP_DIR for the diff view."""
    if not NAME_RE.match(name or ""):
        return {"ok": False, "why": "plugin name: 1-64 chars of [A-Za-z0-9._-]"}
    path = directory / f"{name}.py"
    if not path.is_file():
        return {"ok": False, "why": f"no plugin file {name!r}"}
    data = path.read_bytes()
    current = _sha(data)
    if current != sha256:
        return {"ok": False, "why": "the file changed since you read it — review the new source first",
                "sha256": current}
    manifest = load_manifest(directory)
    manifest[name] = {"sha256": current, "reviewed_at": time.time()}
    save_manifest(directory, manifest)
    snap = directory / SNAP_DIR
    snap.mkdir(parents=True, exist_ok=True)
    (snap / f"{name}@{current[:12]}.py").write_bytes(data)
    return {"ok": True, "sha256": current}


def disable(directory: Path, name: str) -> bool:
    """Drop a plugin from the manifest: it quarantines at the next attach.
    Deleting the whole manifest is the 'revoke all' escape hatch (ADR-0014)."""
    manifest = load_manifest(directory)
    if name not in manifest:
        return False
    del manifest[name]
    save_manifest(directory, manifest)
    return True


def source_view(directory: Path, name: str) -> dict | None:
    """Full current source + a unified diff against the most recent reviewed
    snapshot of the same plugin (the review UI never shows a truncated
    preview — M7a rule)."""
    import difflib
    if not NAME_RE.match(name or ""):
        return None
    path = directory / f"{name}.py"
    if not path.is_file():
        return None
    data = path.read_bytes()
    current = _sha(data)
    text = data.decode("utf-8", errors="replace")
    prev_text = None
    snap = directory / SNAP_DIR
    if snap.is_dir():
        snaps = sorted(snap.glob(f"{name}@*.py"), key=lambda p: p.stat().st_mtime)
        for s in reversed(snaps):
            if not s.name.endswith(f"@{current[:12]}.py"):
                prev_text = s.read_text(encoding="utf-8", errors="replace")
                break
    diff = None
    if prev_text is not None:
        lines = list(difflib.unified_diff(prev_text.splitlines(), text.splitlines(),
                                          fromfile=f"{name} (reviewed)", tofile=f"{name} (current)",
                                          lineterm=""))
        diff = lines or None
    return {"name": name, "sha256": current, "size": len(data), "source": text, "diff": diff}


class LoadedPlugin:
    def __init__(self, name: str, sha256: str, tools: list, hooks: dict) -> None:
        self.name = name
        self.sha256 = sha256
        self.tools = tools
        self.hooks = hooks


def load_plugins(directory: Path) -> tuple[list[LoadedPlugin], list[dict]]:
    """Import ONLY hash-matched plugins. Returns (loaded, failures) where a
    failure is {name, sha256, error} for the panel. A failure never takes
    down the server or another plugin (ADR-0014-7)."""
    loaded: list[LoadedPlugin] = []
    failures: list[dict] = []
    for item in scan(directory):
        if item["state"] != "active":
            continue                      # quarantined/changed: never imported
        path = directory / f"{item['name']}.py"
        mod_name = f"strata_harness_plugin_{item['name']}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, path)
            if spec is None or spec.loader is None:
                raise ImportError("cannot build a module spec")
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                sys.modules.pop(mod_name, None)
                raise
            tools_fn = getattr(module, "tools", None)
            hooks_fn = getattr(module, "hooks", None)
            tools = list(tools_fn()) if callable(tools_fn) else []  # pyright: ignore[reportArgumentType] - contract-checked below
            hooks: dict[str, list] = {}
            if callable(hooks_fn):
                raw = hooks_fn() or {}
                if not isinstance(raw, dict):
                    raise TypeError("hooks() must return a dict")
                for key, fns in raw.items():
                    if key not in HOOK_KEYS:
                        raise ValueError(f"unknown hook key {key!r} (allowed: {', '.join(HOOK_KEYS)})")
                    if not isinstance(fns, list) or not all(callable(f) for f in fns):
                        raise TypeError(f"hooks()[{key!r}] must be a list of callables")
                    hooks[key] = list(fns)
            for t in tools:               # contract check before anything registers
                if not hasattr(t, "name") or not hasattr(t, "execute"):
                    raise TypeError(f"tool {t!r} is not a ToolDefinition")
            loaded.append(LoadedPlugin(item["name"], item["sha256"], tools, hooks))
        except Exception as exc:  # noqa: BLE001 - isolated failure
            failures.append({"name": item["name"], "sha256": item["sha256"],
                             "error": "".join(traceback.format_exception_only(type(exc), exc)).strip()})
    return loaded, failures
