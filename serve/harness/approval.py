"""serve/harness/approval.py — fail-closed approval policy (ADR-0003, ADR-0010).

Closed outcome vocabulary: allow-once | deny | cancelled | unavailable | yolo.
The policy itself is a durable session event (approval/policy), so a resumed
session restores exactly the rights it had — EXCEPT yolo, which never
survives a restart (ADR-0010): fold_policy maps a durable yolo back to ask
on attach. Monotonic guards can only reduce permission, never restore it.

Allowlist matching runs on the FULL arguments (canonical_args), never the
truncated preview (ADR-0010): a pattern must not match text nobody read.
"""
from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field

OUTCOMES = ("allow-once", "deny", "cancelled", "unavailable", "yolo")
POLICIES = ("ask", "never", "allowlist", "yolo")


def canonical_args(tool_name: str, args: dict) -> str:
    """The string allowlist patterns match against: 'tool:command' for shell,
    'tool:path' for file tools, else 'tool:' + compact JSON of the args."""
    if tool_name == "shell":
        return f"shell:{args.get('command', '')}"
    if tool_name in ("read_file", "write_file", "edit_file"):
        return f"{tool_name}:{args.get('path', '')}"
    return f"{tool_name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"


@dataclass
class ApprovalPolicy:
    """Per-session policy, folded from the log. 'ask' = every gated tool asks.
    'never' = gated tools are denied unless the allowlist matches.
    'allowlist' = matched tools run, the rest ask.
    'yolo' = gated tools auto-allow, still logged as decided {yolo} (ADR-0010).
    allowlist = fnmatch patterns over 'tool' or 'tool:<canonical args>'."""
    mode: str = "ask"
    allowlist: list[str] = field(default_factory=list)

    def decide(self, tool_name: str, args: dict) -> str:
        if self.mode == "yolo":
            return "yolo"
        if self.mode == "never":
            return "allow-once" if self._match(tool_name, args) else "deny"
        if self.mode == "allowlist":
            return "allow-once" if self._match(tool_name, args) else "ask"
        return "ask"

    def _match(self, tool_name: str, args: dict) -> bool:
        canonical = canonical_args(tool_name, args)
        return any(fnmatch.fnmatch(tool_name, pat) or fnmatch.fnmatch(canonical, pat)
                   for pat in self.allowlist)


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

    def request(self, req) -> tuple[str, bool]:
        """-> (outcome, asked). asked=False when the policy decided without a human."""
        decision = self.policy.decide(req.tool, req.args)
        if decision in ("allow-once", "yolo", "deny"):
            return decision, False
        if self.answerer is None:
            return "unavailable", True
        try:
            outcome = self.answerer(req)
        except Exception:  # noqa: BLE001
            return "unavailable", True
        return (outcome if outcome in OUTCOMES else "unavailable"), True


def fold_policy(rows: list[dict]) -> ApprovalPolicy:
    """Replay approval/policy events into the current policy.

    A durable yolo never survives a restart (ADR-0010): on attach it folds
    back to ask, so an aggressive mode is always a fresh, deliberate choice
    in the live session."""
    policy = ApprovalPolicy()
    for row in rows:
        if row["type"] == "approval/policy":
            data = row["data"]
            mode = data.get("mode", "ask")
            policy = ApprovalPolicy(mode=mode if mode in POLICIES else "ask",
                                    allowlist=list(data.get("allowlist") or []))
    if policy.mode == "yolo":
        policy = ApprovalPolicy(mode="ask", allowlist=policy.allowlist)
    return policy
