"""serve/harness/inbox.py — two-list inbox: next-turn / next-step (ADR-0004).

Mid-turn steering without a second queue implementation (dsh's idea):
  followup  -> lands at the next turn boundary (queued while a turn runs)
  steer     -> interrupts the current turn at the next step boundary
  inject    -> context only; never wakes the loop
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

TARGETS = ("next-turn", "next-step")
# "subagent": a settled child's result wakes the parent (ADR-0008); it lands
# as a user/message with source="subagent", like followup/goal ride the driver.
PRESETS = ("followup", "steer", "inject", "subagent")


@dataclass
class InboxMessage:
    content: str
    target: str
    preset: str


class Inbox:
    def __init__(self) -> None:
        self._lists: dict[str, list[InboxMessage]] = {t: [] for t in TARGETS}
        self._lock = threading.Lock()
        self._wakeup = threading.Event()

    def send(self, content: str, target: str = "next-turn", preset: str = "followup") -> None:
        if target not in TARGETS or preset not in PRESETS:
            raise ValueError(f"bad inbox address {target}/{preset}")
        with self._lock:
            self._lists[target].append(InboxMessage(content, target, preset))
        if preset != "inject":
            self._wakeup.set()

    def drain(self, target: str) -> list[InboxMessage]:
        with self._lock:
            out, self._lists[target] = self._lists[target], []
        return out

    def pending(self) -> int:
        with self._lock:
            return sum(len(v) for v in self._lists.values())

    def wait_wakeup(self, timeout: float | None = None) -> bool:
        got = self._wakeup.wait(timeout=timeout)
        self._wakeup.clear()
        return got
