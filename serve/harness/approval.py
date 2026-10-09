"""serve/harness/approval.py — fail-closed approval policy (ADR-0003).

Closed outcome vocabulary: allow-once | deny | cancelled | unavailable.
The policy itself is a durable session event (approval/policy), so a resumed
session restores exactly the rights it had. Monotonic guards can only reduce
permission, never restore it.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field

OUTCOMES = ("allow-once", "deny", "cancelled", "unavailable")
POLICIES = ("ask", "never", "allowlist")


@dataclass
class ApprovalPolicy:
    """Per-session policy, folded from the log. 'ask' = every gated tool asks.
    'never' = gated tools are denied unless the allowlist matches.
    allowlist = fnmatch patterns over 'tool' or 'tool:argument-preview'."""
    mode: str = "ask"
    allowlist: list[str] = field(default_factory=list)

    def decide(self, tool_name: str, args_preview: str) -> str:
        if self.mode == "never":
            return "allow-once" if self._match(tool_name, args_preview) else "deny"
        if self.mode == "allowlist":
            return "allow-once" if self._match(tool_name, args_preview) else "ask"
        return "ask"

    def _match(self, tool_name: str, args_preview: str) -> bool:
        for pat in self.allowlist:
            if fnmatch.fnmatch(tool_name, pat) or fnmatch.fnmatch(f"{tool_name}:{args_preview}", pat):
                return True
        return False


class ApprovalBroker:
    """Mediates one ask: policy first, then (if 'ask') the human over the board.

    answerer: (ApprovalRequest) -> "allow-once"|"deny"|"cancelled" (blocks until the
    UI answers or the request is cancelled). A missing/throwing answerer is
    'unavailable' — never an implicit allow.
    """

    def __init__(self, policy: ApprovalPolicy, answerer=None, timeout_s: float = 600.0) -> None:
        self.policy = policy
        self.answerer = answerer
        self.timeout_s = timeout_s

    def request(self, req) -> str:
        decision = self.policy.decide(req.tool, req.args_preview)
        if decision == "allow-once":
            return "allow-once"
        if decision == "deny":
            return "deny"
        if self.answerer is None:
            return "unavailable"
        try:
            outcome = self.answerer(req)
        except Exception:  # noqa: BLE001
            return "unavailable"
        return outcome if outcome in OUTCOMES else "unavailable"


def fold_policy(rows: list[dict]) -> ApprovalPolicy:
    """Replay approval/policy events into the current policy."""
    policy = ApprovalPolicy()
    for row in rows:
        if row["type"] == "approval/policy":
            data = row["data"]
            mode = data.get("mode", "ask")
            policy = ApprovalPolicy(mode=mode if mode in POLICIES else "ask",
                                    allowlist=list(data.get("allowlist") or []))
    return policy
