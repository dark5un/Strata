"""serve/harness/session.py — append-only session log + surface projection (ADR-0004).

One Session = one JSONL file, one writer thread. The log is the whole truth:
LLM message history is derived by folding the surface projection, never
stored separately. Resume repairs crash-orphaned turns by appending closers;
fork copies an exact event prefix.

Storage rules:
  * one JSON row per event, appended; fsync per batch (append_many);
  * a torn tail (last line not parseable) is truncated on open, never on write;
  * unknown event types are kept and skipped by the folder (forward-compat);
  * an unknown header format_version refuses to open (no silent partial replay).
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Callable
from pathlib import Path

from . import HARNESS_FORMAT_VERSION
from . import events as ev


class SessionFormatUnsupported(Exception):
    """The log's format_version is newer than this code can fold."""


class SessionAlreadyOwned(Exception):
    """Another worker holds the write handle for this session."""


class SessionHandle:
    """Single-writer append handle over one session's JSONL log."""

    def __init__(self, path: Path, header: dict, events: list[dict]):
        self.path = path
        self.header = header
        self.events = events          # every event read at open, in seq order
        self._next_seq = (events[-1]["seq"] + 1) if events else 0
        self._dirty: list[dict] = []
        self._lock = threading.RLock()   # appends from parallel tool workers serialize here
        self.on_append: Callable[[dict], None] | None = None   # live hook: (row) -> None (the manager's SSE fan-out)

    def append(self, event_type: str, data: dict, **surface) -> dict:
        with self._lock:
            row = ev.envelope(event_type, data, self._next_seq)
            row.update(surface)           # surfaceOp / sourceEventSeqs ride the envelope
            self._next_seq += 1
            self.events.append(row)
            self._dirty.append(row)
            if self.on_append is not None:
                self.on_append(row)
            return row

    def append_many(self, rows: list[dict]) -> None:
        with self._lock:
            for row in rows:
                self.events.append(row)
                self._dirty.append(row)
            self._next_seq = rows[-1]["seq"] + 1 if rows else self._next_seq

    def flush(self) -> None:
        """Durability barrier: write pending rows, fsync. Readers never see a torn tail."""
        with self._lock:
            if not self._dirty:
                return
            with open(self.path, "a", encoding="utf-8") as f:
                for row in self._dirty:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            self._dirty = []

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._next_seq - 1

    def snapshot(self, since: int = 0) -> list[dict]:
        """A lock-consistent copy of events with seq >= since (for readers/publishers)."""
        with self._lock:
            return [r for r in self.events if r["seq"] >= since]


def new_session_id() -> str:
    return uuid.uuid4().hex[:12]


def create(sessions_dir: Path, session_id: str | None = None, *, cwd: str = "",
           origin: str = "user", parent_session: str | None = None,
           delegation_depth: int = 0, agent_preset: str = "default",
           trusted_roots: list[str] | None = None) -> SessionHandle:
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session_id = session_id or new_session_id()
    path = sessions_dir / f"{session_id}.jsonl"
    if path.exists():
        raise SessionAlreadyOwned(f"session {session_id} already exists")
    header = {
        "format_version": HARNESS_FORMAT_VERSION,
        "id": session_id,
        "cwd": cwd,
        "origin": origin,                    # user | subagent
        "parentSession": parent_session,
        "delegationDepth": delegation_depth,
        "agentPreset": agent_preset,
    }
    if trusted_roots:
        # M9 (ADR-0015): per-session trusted workspaces. Additive field: old
        # headers read as [cwd] (see trusted_roots_of) — no migration.
        header["trustedRoots"] = list(trusted_roots)
    path.write_text(json.dumps({"type": "session/header", "seq": -1, "time": None, "data": header}) + "\n",
                    encoding="utf-8")
    return SessionHandle(path, header, [])


def trusted_roots_of(header: dict) -> list[str] | None:
    """The session's trusted roots, read-compat: no field = the old single
    root (header.cwd); empty cwd = None = the operator's full-surface mode
    (today's root=None posture, unchanged)."""
    roots = header.get("trustedRoots")
    if isinstance(roots, list) and roots:
        return [str(r) for r in roots]
    cwd = str(header.get("cwd") or "")
    return [cwd] if cwd else None


def open_session(sessions_dir: Path, session_id: str) -> SessionHandle:
    path = sessions_dir / f"{session_id}.jsonl"
    if not path.exists():
        raise FileNotFoundError(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    rows: list[dict] = []
    header: dict | None = None
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            # torn tail: rewrite the file with only the good rows (header first), then stop
            with open(path, "w", encoding="utf-8") as f:
                if header is not None:
                    f.write(json.dumps({"type": "session/header", "seq": -1, "time": None, "data": header}) + "\n")
                for good in rows:
                    f.write(json.dumps(good, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            break
        if row.get("type") == "session/header":
            header = row["data"]
            continue
        rows.append(row)
    if header is None:
        raise SessionFormatUnsupported(f"session {session_id}: no header row")
    if int(header.get("format_version", 0)) > HARNESS_FORMAT_VERSION:
        raise SessionFormatUnsupported(
            f"session {session_id}: format_version {header['format_version']} > {HARNESS_FORMAT_VERSION}")
    rows.sort(key=lambda r: r["seq"])
    return SessionHandle(path, header, rows)


def repair_interrupted(handle: SessionHandle) -> list[dict]:
    """Resume repair (ADR-0004): close orphaned turns with durable closers.

    Returns the appended rows. Storage is never rewritten; the agent layer
    decides semantics, we only guarantee balanced brackets.
    """
    open_turn = None
    open_calls: dict[str, dict] = {}
    for row in handle.events:
        t = row["type"]
        if t == ev.TURN_START:
            open_turn = row
            open_calls = {}
        elif t == ev.TURN_END:
            open_turn = None
        elif t == ev.TOOL_CALL:
            open_calls[row["data"]["call_id"]] = row
        elif t == ev.TOOL_RESULT:
            open_calls.pop(row["data"]["call_id"], None)
    closers: list[dict] = []
    for call in open_calls.values():
        closers.append(handle.append(ev.TOOL_RESULT, {
            "call_id": call["data"]["call_id"], "is_error": True,
            "outcome": ev.TOOL_NOT_STARTED, "content": [],
        }))
    if open_turn is not None:
        closers.append(handle.append(ev.TURN_END, {"reason": ev.REASON_INTERRUPTED}))
    handle.flush()
    return closers


def fork(sessions_dir: Path, source: SessionHandle, boundary_seq: int | None = None,
         child_id: str | None = None,
         trusted_roots: list[str] | None = None) -> SessionHandle:
    """Deep-copy an exact event prefix (must end outside an open turn), mark the seed.

    M9 (ADR-0015): the child inherits the parent's trusted roots; a fork may
    NARROW them at creation (a subset), never widen: anything outside the
    parent's set is rejected here.
    """
    prefix = [r for r in source.events if boundary_seq is None or r["seq"] <= boundary_seq]
    depth = 0
    for row in prefix:
        if row["type"] == ev.TURN_START:
            depth += 1
        elif row["type"] == ev.TURN_END:
            depth -= 1
    if depth != 0:
        raise ValueError("fork boundary must end outside an open turn")
    parent_roots = trusted_roots_of(source.header)
    if trusted_roots is None:
        roots = parent_roots
    else:
        if parent_roots is None:
            roots = list(trusted_roots)          # full-surface parent: the fork may set roots
        else:
            pr = {str(Path(p).resolve()) for p in parent_roots}
            for r in trusted_roots:
                rp = Path(str(r)).resolve()
                if not any(str(rp) == p or Path(p) in rp.parents for p in pr):
                    raise ValueError(f"fork roots may only narrow the parent's: {r!r} is outside {sorted(pr)}")
            roots = list(trusted_roots)
    child = create(sessions_dir, child_id, cwd=source.header.get("cwd", ""),
                   origin=source.header.get("origin", "user"),
                   parent_session=source.header["id"],
                   delegation_depth=int(source.header.get("delegationDepth", 0)),
                   agent_preset=source.header.get("agentPreset", "default"),
                   trusted_roots=roots)
    renumbered = []
    for seq, row in enumerate(prefix):
        new_row = dict(row)
        new_row["seq"] = seq
        renumbered.append(new_row)
    child.append_many(renumbered)
    child.append(ev.SESSION_END_SEED, {"inherited": True, "source": source.header["id"],
                                       "source_last_seq": prefix[-1]["seq"] if prefix else -1,
                                       "inherited_event_count": len(prefix)})
    child.flush()
    return child


# --------------------------------------------------------------------------- surface

def fold_surface(rows: list[dict]) -> list[dict]:
    """Project the log to the ordered surface nodes the model sees.

    'append' nodes land in order; a replace node removes the surface nodes
    whose seqs fall in [startSeq, endSeq] and lands in their place. Unknown
    types are skipped. Tool brackets ride their messages' data.
    """
    surface: list[dict] = []
    for row in rows:
        op = row.get("surfaceOp", "append")
        insert_at: int | None = None
        if isinstance(op, dict) and op.get("op") == "replace":
            lo, hi = op["startSeq"], op["endSeq"]
            # the shadow node lands where the replaced span began, not at the end:
            # derived history keeps chronological order (summary first, then kept turns)
            insert_at = next((i for i, n in enumerate(surface) if n["seq"] >= lo), len(surface))
            surface = [n for n in surface if not (lo <= n["seq"] <= hi)]
        if row["type"] in ev.MESSAGE_TYPES or row["type"] == ev.COMPACTION_SUMMARY or row["type"] == ev.TOOL_RESULT:
            if insert_at is not None:
                surface.insert(insert_at, row)
            else:
                surface.append(row)
    return surface


def derive_messages(handle: SessionHandle) -> list[dict]:
    """The LLM history: OpenAI-shaped messages folded from the surface."""
    messages: list[dict] = []
    for row in fold_surface(handle.events):
        data = row["data"]
        if row["type"] == ev.USER_MESSAGE or row["type"] == ev.COMPACTION_SUMMARY:
            messages.append({"role": "user", "content": data.get("content", "")})
        elif row["type"] == ev.ASSISTANT_MESSAGE:
            msg: dict = {"role": "assistant", "content": data.get("content") or ""}
            calls = data.get("tool_calls")
            if calls:
                msg["tool_calls"] = calls
            messages.append(msg)
        elif row["type"] == ev.TOOL_RESULT:
            messages.append({"role": "tool", "tool_call_id": data["call_id"],
                             "content": data.get("text", "")})
    return messages
