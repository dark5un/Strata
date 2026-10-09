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
from .approval import ApprovalBroker, ApprovalPolicy, fold_policy
from .compaction import orphaned_bracket, prune_tool_results
from .inbox import Inbox
from .loop import Loop
from .modes import GOAL_PHASES, fold_goal, fold_plan, goal_rounds_used, goal_section, plan_section
from .presets import load_preset
from .session import SessionHandle, create, fork, open_session, repair_interrupted
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


class HarnessManager:
    def __init__(self, sessions_dir: Path, engine_factory, *, spill_dir: Path | None = None,
                 workspace_root: Path | None = None, metrics_fn=None,
                 presets_dir: Path | None = None) -> None:
        self.dir = sessions_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.engine_factory = engine_factory          # () -> engine callable
        self.spill_dir = spill_dir or (sessions_dir.parent / "spill")
        self.workspace_root = workspace_root
        self.metrics_fn = metrics_fn                  # Service.metrics(): the monitor JSON (ADR-0002)
        self.presets_dir = presets_dir                # None -> serve/harness/presets/ (ADR-0009)
        self._live: dict[str, Live] = {}
        self._lock = threading.RLock()   # reentrant: live() -> _attach() reads the live map for parent lookups

    # ---------------------------------------------------------------- lifecycle

    def new(self, session_id: str | None = None, *, preset: str = "default") -> str:
        h = create(self.dir, session_id, cwd=str(self.workspace_root or Path.cwd()), agent_preset=preset)
        return h.header["id"]

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
                            "preset": h.get("agentPreset", "default")})
                continue
            try:
                h = open_session(self.dir, sid)
            except Exception:  # noqa: BLE001 - a broken log still lists, flagged
                out.append({"id": sid, "busy": False, "events": -1, "error": "unreadable"})
                continue
            out.append({"id": sid, "busy": False, "events": len(h.events),
                        "origin": h.header.get("origin", "user"), "parent": h.header.get("parentSession"),
                        "preset": h.header.get("agentPreset", "default")})
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
                        parent_effective = {t.name for t in default_registry(root=self.workspace_root)
                                            if ppreset.permits(t.name)}
                except Exception:  # noqa: BLE001 - unreadable parent log: no extra restriction
                    pass
        tools = {t.name: t for t in default_registry(board=board, root=self.workspace_root)}
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
        loop = Loop(engine=self.engine_factory(), tools=tools, session=h,
                    approval=broker.request,
                    spill=spill_store,
                    inbox=Inbox(),
                    metrics_fn=self.metrics_fn,
                    system_sections=[lambda h=h: plan_section(fold_plan(h.snapshot())),
                                     lambda h=h: goal_section(fold_goal(h.snapshot())),
                                     lambda p=preset: p.section()])
        loop._policy = policy                     # type: ignore[attr-defined]  # routes update it
        loop._broker = broker                     # type: ignore[attr-defined]
        loop._board = board                       # type: ignore[attr-defined]
        live = Live(session_id=session_id, handle=h, loop=loop, board=board, approvals=approvals,
                    channel=channel, deliver=channel.deliver)
        h.on_append = live.publish                # every durable row streams the moment it lands
        return live

    def fork(self, session_id: str, boundary_seq: int | None = None) -> str:
        src = open_session(self.dir, session_id)
        child = fork(self.dir, src, boundary_seq)
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
                       agent_preset=preset)
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
        live.loop._broker.policy = ApprovalPolicy(mode, allowlist)   # type: ignore[attr-defined]

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
        for row in live.handle.snapshot():
            q.put(row)
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

    def __init__(self, notices: queue.Queue[dict]) -> None:
        self.notices = notices
        self._decisions = {}
        self._asks = {}
        self._lock = threading.Lock()

    def answerer(self, req) -> str:
        dq: queue.Queue = queue.Queue()
        with self._lock:
            self._decisions[req.call_id] = dq
            self._asks[req.call_id] = {"call_id": req.call_id, "tool": req.tool, "preview": req.args_preview}
        self.notices.put({"call_id": req.call_id, "tool": req.tool, "preview": req.args_preview})
        if self.relay is not None:
            parent_notices, child_sid = self.relay
            parent_notices.put({"call_id": req.call_id, "tool": req.tool, "preview": req.args_preview,
                                "session": child_sid})
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
