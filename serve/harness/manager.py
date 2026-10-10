"""serve/harness/manager.py — owns live sessions: workers, boards, event fan-out.

The HTTP routes (routes.py) and the CLI driver (cli.py) both go through a
HarnessManager. One manager per server process. A live session =
SessionHandle + Loop + worker thread + QuestionBoard + approval queue +
subscriber list (SSE connections).

Threading model (ADR-0003): one worker thread per session runs the loop;
routes never call the engine directly. A crash in a worker settles the turn
with an error event; the server itself is never taken down.
"""
from __future__ import annotations

import contextlib
import queue
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import events as ev
from .approval import ApprovalBroker, ApprovalPolicy, fold_policy, targets_protected
from .compaction import orphaned_bracket, prune_tool_results
from .inbox import Inbox
from .loop import Loop
from .modes import GOAL_PHASES, fold_goal, fold_plan, goal_rounds_used, goal_section, plan_section
from .presets import load_preset
from .session import SessionHandle, create, fork, open_session, repair_interrupted, trusted_roots_of
from .spill import SpillStore
from .subagents import budget_check, child_result_text, delegate_tool
from .tools import default_registry
from .tools.ask import QuestionBoard
from .tools.modes import goal_tool, plan_mode_tool
from .tools.spill import spill_read_tool


@dataclass
class Live:
    session_id: str
    handle: SessionHandle
    loop: Loop
    board: QuestionBoard
    approvals: queue.Queue[dict]
    channel: ApprovalChannel | None = None
    deliver: object = None                # answerer-side: deliver(call_id, outcome) -> bool
    worker: threading.Thread | None = None
    subs: list[queue.Queue] = field(default_factory=list)
    sub_lock: threading.Lock = field(default_factory=threading.Lock)
    busy: bool = False
    children: set[str] = field(default_factory=set)   # live subagent sessions (ADR-0008)
    on_idle: Callable[[], None] | None = None       # settle hook: called when the driver chain ends

    def publish(self, row: dict) -> None:
        with self.sub_lock:
            for q in self.subs:
                q.put(row)


def _session_title(events: list[dict]) -> str:
    """A human label for the session list: the durable model title when one
    exists (M7 D10), else the first real user message, one line, truncated.
    Forks inherit it (the prefix carries both); subagent-sourced messages
    don't title their parent."""
    for row in reversed(events):
        if row["type"] == ev.SESSION_TITLE:
            text = str((row.get("data") or {}).get("text", "")).strip()
            if text:
                return " ".join(text.split())[:60]
    for row in events:
        if row["type"] == ev.USER_MESSAGE and not (row.get("data") or {}).get("source"):
            text = str((row.get("data") or {}).get("content", "")).strip()
            if text:
                return " ".join(text.split())[:60]
    return ""


class HarnessManager:
    def __init__(self, sessions_dir: Path, engine_factory, *, spill_dir: Path | None = None,
                 workspace_root: Path | None = None, metrics_fn=None,
                 presets_dir: Path | None = None, sampling: dict | None = None,
                 mcp_hub=None, plugins_dir: Path | None = None,
                 default_roots: list[str] | None = None) -> None:
        self.dir = sessions_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.engine_factory = engine_factory          # () -> engine callable
        self.spill_dir = spill_dir or (sessions_dir.parent / "spill")
        self.workspace_root = workspace_root
        self.metrics_fn = metrics_fn                  # Service.metrics(): the monitor JSON (ADR-0002)
        self.presets_dir = presets_dir                # None -> user dir + bundled (ADR-0011)
        self.sampling = dict(sampling or {})          # rides the request/header envelope (M7b)
        self.mcp_hub = mcp_hub                        # serve/mcp.py's McpHub when MCP servers are set (ADR-0012)
        self.plugins_dir = plugins_dir                # None = plugins off (ADR-0014: opt-in at server start)
        # M9: the operator's default trusted folders (STRATA_HARNESS_ROOTS,
        # os.pathsep-joined). A new session with no explicit roots starts
        # fenced to these instead of the bare workspace_root — the container
        # deployment sets this to /data/workspaces so the picker's tree is
        # the fence from the first message. Validated at construction; an
        # unusable entry is dropped (server start must not die on a typo).
        self.default_roots: list[str] | None = None
        if default_roots:
            out = []
            for r in default_roots:
                p = Path(str(r)).expanduser().resolve()
                if p.is_dir():
                    out.append(str(p))
            self.default_roots = out or None
        self._plugin_failures: list[dict] = []        # last attach's import/contract errors (panel state)
        self._plugin_tool_names: list[str] = []       # last attach's reviewed plugin tool names
        self._live: dict[str, Live] = {}
        self._lock = threading.RLock()   # reentrant: live() -> _attach() reads the live map for parent lookups

    # ---------------------------------------------------------------- lifecycle

    def new(self, session_id: str | None = None, *, preset: str = "default",
            roots: list[str] | None = None) -> str:
        # M9 (ADR-0015): roots = the session's trusted folders (subfolders
        # included). Validated at creation: each must exist and be a directory
        # (the fence resolves paths against them; a typo'd root would silently
        # deny everything otherwise). None = today's server-default workspace.
        trusted: list[str] | None = None
        if roots is not None:
            if not isinstance(roots, list) or not roots:
                raise ValueError("roots: a non-empty list of folder paths")
            out = []
            for r in roots:
                p = Path(str(r)).expanduser().resolve()
                if not p.is_dir():
                    raise ValueError(f"trusted root {str(r)!r} is not a directory")
                out.append(str(p))
            trusted = out
        elif self.default_roots:
            # operator default (M9): a session created without explicit roots
            # is fenced to the deployment's trusted tree, not the bare cwd
            trusted = list(self.default_roots)
        h = create(self.dir, session_id, cwd=str(self.workspace_root or Path.cwd()), agent_preset=preset,
                   trusted_roots=trusted)
        return h.header["id"]

    def _session_roots(self, h) -> list[str] | None:
        """The fence for one session: its trusted roots (M9), read-compat to
        [header.cwd]; None only when the header carries no cwd at all
        (the operator's full-surface posture, unchanged)."""
        roots = trusted_roots_of(h.header)
        if roots is None:
            return None
        return [str(Path(r).expanduser().resolve()) for r in roots]

    def list_sessions(self) -> list[dict]:
        out = []
        for path in sorted(self.dir.glob("*.jsonl")):
            sid = path.stem
            with self._lock:
                live = self._live.get(sid)
            if live is not None:
                h = live.handle.header
                out.append({"id": sid, "busy": live.busy, "events": len(live.handle.events),
                            "origin": h.get("origin", "user"), "parent": h.get("parentSession"),
                            "preset": h.get("agentPreset", "default"),
                            "roots": h.get("trustedRoots"),   # M9: raw field — None = server default
                            "title": _session_title(live.handle.events)})
                continue
            try:
                h = open_session(self.dir, sid)
            except Exception:  # noqa: BLE001 - a broken log still lists, flagged
                out.append({"id": sid, "busy": False, "events": -1, "error": "unreadable"})
                continue
            out.append({"id": sid, "busy": False, "events": len(h.events),
                        "origin": h.header.get("origin", "user"), "parent": h.header.get("parentSession"),
                        "preset": h.header.get("agentPreset", "default"),
                        "roots": h.header.get("trustedRoots"),   # M9: raw — None = server default
                        "title": _session_title(h.events)})
        return out

    def live(self, session_id: str) -> Live:
        with self._lock:
            existing = self._live.get(session_id)
            if existing is not None:
                return existing
            live = self._attach(session_id)
            self._live[session_id] = live
            return live

    def _attach(self, session_id: str) -> Live:
        h = open_session(self.dir, session_id)
        repair_interrupted(h)
        orphan = orphaned_bracket(h)
        if orphan is not None:                    # crash mid-compaction: close the bracket
            h.append(ev.COMPACTION_END, {"committed": False, "repaired": True})
            h.flush()
        board = QuestionBoard()
        approvals: queue.Queue[dict] = queue.Queue()
        channel = ApprovalChannel(approvals)
        policy = fold_policy(h.events)
        broker = ApprovalBroker(policy, answerer=channel.answerer)
        # preset (ADR-0009): persona rides system_sections; tools.allow/deny
        # filter the registry. Subagent presets intersect with the parent's
        # effective set: a child never gains a tool its parent lacked.
        preset, preset_warning = load_preset(h.header.get("agentPreset", "default"), self.presets_dir)
        if preset_warning:
            h.append(ev.PRESET_WARNING, {"preset": h.header.get("agentPreset"), "why": preset_warning})
            h.flush()
        parent_effective: set[str] | None = None
        if h.header.get("origin") == "subagent" and h.header.get("parentSession"):
            with self._lock:
                parent = self._live.get(h.header["parentSession"])
            if parent is not None:
                parent_effective = set(parent.loop.tools)
            else:
                # restart: the parent is not attached yet — intersect with the
                # parent's preset restrictions read from its header (ADR-0009)
                try:
                    ph = open_session(self.dir, h.header["parentSession"]).header
                    ppreset, _ = load_preset(ph.get("agentPreset", "default"), self.presets_dir)
                    if ppreset.allow or ppreset.deny:
                        names = {t.name for t in default_registry(root=self.workspace_root)}
                        if self.mcp_hub is not None:
                            names |= {t["name"] for t in self.mcp_hub.template_tools()}
                        if self.plugins_dir is not None:
                            # reviewed plugin tools exist in the parent's set too (ADR-0014)
                            names |= set(self._plugin_tool_names)
                        parent_effective = {n for n in names if ppreset.permits(n)}
                except Exception:  # noqa: BLE001 - unreadable parent log: no extra restriction
                    pass
        tools = {t.name: t for t in default_registry(board=board, root=self._session_roots(h))}
        # M9: the shell's default cwd is the session's FIRST trusted root;
        # an explicit cwd argument is fenced to the roots inside the tool.
        roots = self._session_roots(h)
        if roots:
            shell = tools.get("shell")
            if shell is not None:
                from .tools.shell import shell_tool
                tools["shell"] = shell_tool(cwd=roots[0], roots=roots)
        if self.mcp_hub is not None:
            # ADR-0012: the hub's ready tools join the registry, gated like any
            # machine tool; snapshot at attach (no hot-reload mid-session).
            from .tools.mcp import mcp_tools
            for t in mcp_tools(self.mcp_hub):
                tools.setdefault(t.name, t)
        # ADR-0014: reviewed plugins only (hash-gated). Tools register into the
        # same dict — a collision with a built-in is a hard failure for that
        # plugin, never a shadow. Hooks ride the ADR-0013 lists. Snapshotted
        # at attach like MCP tools: a review takes effect for new sessions.
        plugin_tools: set[str] = set()
        plugin_hooks: dict[str, list] = {}
        if self.plugins_dir is not None:
            from . import plugins as _plugins
            loaded, failures = _plugins.load_plugins(self.plugins_dir)
            self._plugin_failures = failures
            for p in loaded:
                try:
                    for t in p.tools:
                        if t.name in tools:
                            raise ValueError(f"plugin {p.name!r} tool {t.name!r} collides with an existing tool")
                        tools[t.name] = t
                        plugin_tools.add(t.name)
                except Exception as exc:  # noqa: BLE001 - contract failure: skip this plugin only
                    self._plugin_failures.append({"name": p.name, "sha256": p.sha256, "error": str(exc)})
                    continue
                for key in ("pre_step", "pre_tool", "post_tool"):
                    plugin_hooks.setdefault(key, []).extend(p.hooks.get(key, []))
            self._plugin_tool_names = sorted(plugin_tools)
        spill_store = SpillStore(self.spill_dir / session_id)
        for extra in (spill_read_tool(spill_store), plan_mode_tool(board), goal_tool()):
            extra.needs_approval = False      # session-state tools: they touch no machine state
            tools[extra.name] = extra
        if h.header.get("origin") != "subagent":
            # only user sessions delegate (the depth budget is the backstop;
            # ADR-0009: a preset may still deny it)
            tools["delegate"] = delegate_tool(
                lambda task, preset_name, _sid=session_id: self.spawn_child(_sid, task, preset_name))
        if preset.allow or preset.deny or parent_effective is not None:
            for name in list(tools):
                ok = preset.permits(name) and (parent_effective is None or name in parent_effective)
                if not ok:
                    del tools[name]
        # the loop's stream sink publishes through the Live once it exists (M7a-3)
        live_holder: dict = {"v": None}
        loop = Loop(engine=self.engine_factory(), tools=tools, session=h,
                    approval=broker.request,
                    spill=spill_store,
                    inbox=Inbox(),
                    metrics_fn=self.metrics_fn,
                    sampling=self.sampling,
                    on_stream=lambda d, live_ref=live_holder: live_ref["v"].publish(
                        {"type": "stream/delta", "seq": -1, "time": time.time(), "data": d})
                    if live_ref["v"] is not None else None,
                    system_sections=[lambda h=h: plan_section(fold_plan(h.snapshot())),
                                     lambda h=h: goal_section(fold_goal(h.snapshot())),
                                     lambda p=preset: p.section()])
        loop._policy = policy                     # type: ignore[attr-defined]  # routes update it
        loop._broker = broker                     # type: ignore[attr-defined]
        loop._board = board                       # type: ignore[attr-defined]
        # ADR-0014 gate 2: writes into the plugin dir always ask, whatever the
        # session's policy mode (the durable policy event never carries this —
        # it is server-scoped, set fresh at every attach).
        if self.plugins_dir is not None:
            broker.policy.protected_dirs = [self.plugins_dir]
            channel.protected_dirs = [self.plugins_dir]   # the card's `plugin write` chip (D4)
        for key in ("pre_step", "pre_tool", "post_tool"):   # ADR-0013 hook surface
            getattr(loop, key).extend(plugin_hooks.get(key, []))
        live = Live(session_id=session_id, handle=h, loop=loop, board=board, approvals=approvals,
                    channel=channel, deliver=channel.deliver)
        if plugin_tools:
            channel.plugin_tools = plugin_tools   # the ask card gets a `plugin` chip (ADR-0014)
        live_holder["v"] = live
        h.on_append = live.publish                # every durable row streams the moment it lands
        return live

    def fork(self, session_id: str, boundary_seq: int | None = None,
             roots: list[str] | None = None) -> str:
        # M9: a fork inherits the parent's trusted roots; `roots` may narrow
        # them (session.fork rejects any widening).
        src = open_session(self.dir, session_id)
        child = fork(self.dir, src, boundary_seq, trusted_roots=roots)
        return child.header["id"]

    # ---------------------------------------------------------------- commands

    def send(self, session_id: str, content: str, *, target: str = "auto") -> None:
        live = self.live(session_id)
        if live.busy:
            # mid-turn: steer via the inbox instead of a second turn (dsh semantics).
            # target=steer interrupts at the next step; the default follows up next turn.
            preset = "steer" if target == "steer" else "followup"
            if live.loop.inbox is not None:
                live.loop.inbox.send(content, target="next-step" if preset == "steer" else "next-turn",
                                     preset=preset)
            live.publish({"type": "inbox/queued", "seq": -1, "time": None,
                          "data": {"content": content[:200], "preset": preset}})
            return
        self._start(live, content)

    def _start(self, live: Live, content: str, *, source: str | None = None) -> None:
        live.busy = True
        live.worker = threading.Thread(target=self._drive, args=(live, content, source), daemon=True)
        live.worker.start()

    def _maybe_title(self, live: Live) -> None:
        """M7 D10: name the session with a short model summary after its first
        completed turn. Best-effort: any failure leaves the truncation fallback
        (_session_title reads the first user message). One call per session:
        a durable session/title row means we never regenerate."""
        h = live.handle
        if h.header.get("origin") != "subagent" and not any(
                r["type"] == ev.SESSION_TITLE for r in h.events):
            first = next((str(r["data"].get("content", "")) for r in h.events
                          if r["type"] == ev.USER_MESSAGE and not r["data"].get("source")), "")
            if not first:
                return
            try:
                raw = live.loop.engine(
                    [{"role": "user", "content": "Reply with a title for this conversation: "
                     "2-6 words, lowercase, no punctuation, no quotes.\n\n" + first[:500]}], None)
                from .loop import normalize_result
                text = normalize_result(raw).content.strip().strip('"').strip()
                text = " ".join(text.split())[:60]
                if text and len(text) > 1:
                    h.append(ev.SESSION_TITLE, {"text": text, "by": "model"})
                    h.flush()
                    live.publish(h.snapshot()[-1])
            except Exception:  # noqa: BLE001 - a title is never worth an error row
                pass

    def _drive(self, live: Live, first_content: str, first_source: str | None = None) -> None:
        """Turn driver: run the first turn, then keep the session moving at idle
        points — queued followups first, then goal rounds while the goal is active.
        One worker thread per session: this loop is the single writer."""
        h = live.handle
        content: str | None = first_content
        source: str | None = first_source
        try:
            while content is not None:
                # context pressure: prune oversized results before deriving (cheap, no model call)
                prune_tool_results(h)
                if source is None:
                    live.loop.send(content)
                else:
                    h.append(ev.USER_MESSAGE, {"content": content, "source": source})
                    live.loop.run_turn()
                self._maybe_title(live)
                content, source = None, None
                if live.loop.cancel.is_set():
                    break                       # a cancelled turn stops the driver chain too
                if live.loop.inbox is not None:
                    queued = live.loop.inbox.drain("next-turn")
                    queued = [m for m in queued if m.preset != "inject"]
                    if queued:
                        content, source = queued[0].content, queued[0].preset
                        for extra in queued[1:]:
                            live.loop.inbox.send(extra.content, "next-turn", extra.preset)
                        continue
                content = self._next_goal_round(live)
                if content is not None:
                    source = "goal"
        except Exception:  # noqa: BLE001 - supervisor: settle, never kill the server
            try:
                h.append(ev.TURN_END, {"reason": ev.REASON_ERROR, "message": traceback.format_exc(limit=3)})
                h.flush()
            except Exception:  # noqa: BLE001
                pass
        finally:
            live.busy = False
            # rows reach subscribers live through handle.on_append (no end-of-turn bulk);
            # this synthetic notice lands AFTER the flag clears, so the UI's session
            # list never reads a stale busy=True next to a fresh turn/end
            live.publish({"type": "session/idle", "seq": -1, "time": time.time(),
                          "data": {"session": live.session_id}})
            if live.on_idle is not None:              # subagent settle hook (ADR-0008)
                hook = live.on_idle
                live.on_idle = None
                with contextlib.suppress(Exception):   # a settle failure must not kill the worker
                    hook()

    def _next_goal_round(self, live: Live) -> str | None:
        """Goal round driver (ADR-0007): one goal-sourced message per idle point
        while phase=active and the round budget holds. Never revives after a
        restart: the budget folds from the log, but a fresh attach does not
        auto-start — only an idle point inside a live driver chain queues rounds."""
        goal = fold_goal(live.handle.snapshot())
        if not goal or goal["phase"] != "active":
            return None
        if goal_rounds_used(live.handle.snapshot()) >= goal["maxRounds"]:
            live.handle.append(ev.GOAL_CHANGE, {**goal, "phase": "blocked",
                                                "why": "maxRounds"})
            live.handle.flush()
            return None
        return (f"Continue working toward the goal: {goal['objective']} "
                "When it is fully achieved, call the goal tool with phase=complete.")

    def cancel(self, session_id: str) -> bool:
        live = self._live.get(session_id)
        if live is None or not live.busy:
            return False
        live.loop.request_cancel()
        live.board.cancel_all()
        if live.channel is not None:            # a blocked approval must not hold the worker
            live.channel.cancel_pending()
        for child_sid in list(live.children):   # cancel cascades to running subagents (ADR-0008)
            self.cancel(child_sid)
        return True

    def delete(self, session_id: str) -> bool:
        """Delete a session: cancel any running turn, drop the live handle,
        remove the JSONL log. The log is the whole truth (ADR-0004), so
        removing it IS the delete — no tombstone. Children keep their own
        logs; their parent pointer simply stops resolving (the list row
        already tolerates an unreadable parent). Spill files are content-
        addressed and shared across sessions: never touched here."""
        self.cancel(session_id)
        with self._lock:
            live = self._live.pop(session_id, None)
        path = self.dir / f"{session_id}.jsonl"
        if live is None and not path.exists():
            raise FileNotFoundError(session_id)
        path.unlink(missing_ok=True)
        return True

    # ---------------------------------------------------------------- subagents (ADR-0008)

    def spawn_child(self, parent_id: str, task: str, preset: str) -> tuple[str | None, str | None]:
        """Create + start a subagent session. Returns (child_id, None) or
        (None, refusal_reason). Budgets fold from the parent's log, so they
        survive restarts."""
        parent = self.live(parent_id)
        refusal = budget_check(parent.handle.snapshot(), int(parent.handle.header.get("delegationDepth", 0)))
        if refusal is not None:
            return None, refusal
        child = create(self.dir, cwd=str(self.workspace_root or Path.cwd()),
                       origin="subagent", parent_session=parent_id,
                       delegation_depth=int(parent.handle.header.get("delegationDepth", 0)) + 1,
                       agent_preset=preset,
                       # M9: subagents inherit the parent's trusted roots —
                       # a child can never widen the fence it was born inside.
                       trusted_roots=trusted_roots_of(parent.handle.header))
        child_id = child.header["id"]
        # create() already wrote the header row to disk; _attach reopens the log
        parent.handle.append(ev.SUBAGENT_SPAWN, {"child": child_id, "preset": preset,
                                                 "depth": child.header["delegationDepth"]})
        parent.handle.flush()
        live = self.live(child_id)               # attach (preset intersection reads the live parent)
        if live.channel is not None:
            live.channel.relay = (parent.approvals, child_id)   # asks surface in the parent's stream
        parent.children.add(child_id)
        live.on_idle = lambda: self._settle_child(parent_id, child_id)
        self._start(live, task)
        return child_id, None

    def _settle_child(self, parent_id: str, child_id: str) -> None:
        """The child's driver chain ended: deliver its result to the parent as a
        subagent-sourced message and wake the parent if it is idle."""
        with self._lock:
            parent = self._live.get(parent_id)
        if parent is None:
            return
        parent.children.discard(child_id)
        try:
            child = self.live(child_id)
            text = child_result_text(child.handle)
        except Exception:  # noqa: BLE001
            text = f"[subagent {child_id} settled with an unreadable log]"
        if parent.busy:
            if parent.loop.inbox is not None:
                parent.loop.inbox.send(text, target="next-turn", preset="subagent")
            return
        self._start(parent, text, source="subagent")

    def plugin_failures(self) -> list[dict]:
        """Import/contract errors from the most recent attach (panel state, ADR-0014-7)."""
        return list(self._plugin_failures)

    def refresh_plugins(self) -> None:
        """Re-run the reviewed load after a human review/disable (POST path):
        keeps the cached tool names honest without waiting for a new attach."""
        if self.plugins_dir is None:
            return
        from . import plugins as _plugins
        loaded, failures = _plugins.load_plugins(self.plugins_dir)
        self._plugin_failures = failures
        self._plugin_tool_names = sorted(t.name for p in loaded for t in p.tools)

    def active_plugin_tools(self) -> list[str]:
        """Tool names of hash-reviewed plugins, cached from the last attach
        (GET /harness/tools must not re-import anything)."""
        return list(self._plugin_tool_names)

    def pending_child_asks(self, session_id: str) -> list[dict]:
        """Relayed asks (ADR-0008): pending approvals of live children, tagged
        with their session id — a reconnecting parent client redraws them."""
        with self._lock:
            parent = self._live.get(session_id)
            kids = [self._live.get(c) for c in (parent.children if parent else ())]
        out = []
        for child in kids:
            if child is not None and child.channel is not None:
                for a in child.channel.pending():
                    out.append({**a, "session": child.session_id})
        return out

    def answer(self, session_id: str, qid: str, answer: dict) -> bool:
        return self.live(session_id).board.answer(qid, answer)

    def decide(self, session_id: str, call_id: str, outcome: str) -> bool:
        if outcome not in ("allow-once", "deny", "cancelled"):
            return False
        live = self.live(session_id)
        deliver = live.deliver
        if callable(deliver) and deliver(call_id, outcome):
            return True
        # relayed asks (ADR-0008): the call_id may belong to a child session
        for child_sid in list(live.children):
            child = self._live.get(child_sid)
            if child is not None and callable(child.deliver) and child.deliver(call_id, outcome):
                return True
        return False

    def set_policy(self, session_id: str, mode: str, allowlist: list[str]) -> None:
        live = self.live(session_id)
        live.handle.append("approval/policy", {"mode": mode, "allowlist": allowlist})
        live.handle.flush()
        # the live session runs the mode as set; a yolo never survives a restart
        # because fold_policy (attach) maps a durable yolo back to ask (ADR-0010)
        new_policy = ApprovalPolicy(mode, allowlist)
        if self.plugins_dir is not None:            # ADR-0014: the write gate is not a session setting
            new_policy.protected_dirs = [self.plugins_dir]
        live.loop._broker.policy = new_policy       # type: ignore[attr-defined]

    def set_mode(self, session_id: str, *, plan: bool | None = None,
                 goal: dict | None = None) -> None:
        """User-side mode switches (ADR-0007): durable events only; the loop picks
        them up at the next derive (system sections) or idle point (goal driver)."""
        live = self.live(session_id)
        if plan is not None:
            live.handle.append(ev.PLAN_MODE, {"active": bool(plan), "by": "user"})
        if goal is not None:
            phase = goal.get("phase", "active")
            live.handle.append(ev.GOAL_CHANGE, {
                "objective": str(goal.get("objective", "")),
                "phase": phase if phase in GOAL_PHASES else "active",
                "maxRounds": int(goal.get("maxRounds", 8)), "by": "user"})
        live.handle.flush()

    # ---------------------------------------------------------------- events

    def subscribe(self, session_id: str) -> queue.Queue:
        live = self.live(session_id)
        q: queue.Queue[dict] = queue.Queue()
        with live.sub_lock:
            live.subs.append(q)
        rows = live.handle.snapshot()
        for row in rows:
            q.put(row)
        # the pill's source of truth survives a reload: fold the meter once at
        # subscribe time (the same fold that drives compaction) and send it as
        # the synthetic context delta the live loop publishes per step
        if self.metrics_fn is not None:
            from .meter import context_pressure, fold_meter
            est = fold_meter(rows)["prompt_estimate"]
            q.put({"type": "stream/delta", "seq": -1, "time": time.time(),
                   "data": {"kind": "context", "estimate": est,
                            "pressure": round(context_pressure(self.metrics_fn(), est), 3)}})
        return q

    def unsubscribe(self, session_id: str, q: queue.Queue) -> None:
        live = self._live.get(session_id)
        if live is None:
            return
        with live.sub_lock:
            if q in live.subs:
                live.subs.remove(q)


class ApprovalChannel:
    """Ask and decision are separate channels: the answerer publishes the ask
    to `notices` (the UI/SSE side reads it) and blocks on a per-call decision
    queue fed by deliver(). The answerer never consumes its own notification.

    relay (ADR-0008): a subagent's channel can mirror its asks into the
    parent's notices queue — one place to answer, no tab-hunting."""
    notices: queue.Queue[dict]
    _decisions: dict[str, queue.Queue]
    _asks: dict[str, dict]
    _lock: threading.Lock
    relay: tuple[queue.Queue[dict], str] | None = None   # (parent notices, child session id)
    plugin_tools: set[str] = set()                       # ADR-0014: ask cards get a `plugin` chip
    protected_dirs: list = []                            # ADR-0014: a write here gets the `plugin write` chip

    def __init__(self, notices: queue.Queue[dict]) -> None:
        self.notices = notices
        self._decisions = {}
        self._asks = {}
        self._lock = threading.Lock()

    def answerer(self, req) -> str:
        dq: queue.Queue = queue.Queue()
        ask = {"call_id": req.call_id, "tool": req.tool, "args": req.args,
               "preview": req.args_preview}          # full args: the card shows what runs (ADR-0010)
        if req.tool in self.plugin_tools:            # a plugin's own tool: chip it (D4)
            ask["plugin"] = True
        if targets_protected(req.tool, req.args, self.protected_dirs):   # a write into the plugin dir: chip + copy (D4)
            ask["plugin_write"] = True
        with self._lock:
            self._decisions[req.call_id] = dq
            self._asks[req.call_id] = ask
        self.notices.put(ask)
        if self.relay is not None:
            parent_notices, child_sid = self.relay
            parent_notices.put({**ask, "session": child_sid})
        try:
            decision = dq.get(timeout=3600)
        except queue.Empty:
            decision = {"outcome": "unavailable"}
        finally:
            with self._lock:
                self._decisions.pop(req.call_id, None)
                self._asks.pop(req.call_id, None)
        return decision["outcome"]

    def deliver(self, call_id: str, outcome: str) -> bool:
        with self._lock:
            dq = self._decisions.get(call_id)
            # a decided ask stops being pending right here: a reconnecting
            # client must not redraw a card the human already answered
            self._asks.pop(call_id, None)
        if dq is None:
            return False
        dq.put({"outcome": outcome})
        return True

    def pending(self) -> list[dict]:
        """Unanswered asks: a reconnecting client reconciles with these."""
        with self._lock:
            return list(self._asks.values())

    def cancel_pending(self) -> None:
        """A cancelled turn must not leave the worker blocked on an ask."""
        with self._lock:
            dqs = list(self._decisions.values())
        for dq in dqs:
            dq.put({"outcome": "cancelled"})
