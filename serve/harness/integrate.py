"""serve/harness/integrate.py — the single wiring point between server.py and the harness.

ADR-0001 keeps the fork's cut points in upstream files additive and small.
This module holds everything the harness needs at startup so server.py only
carries a two-line call: route branches (GET/POST), the --harness flag, and
`integrate.attach(svc, a, cfg, hub)`. Upstream churn in main() then cannot
touch the harness's construction logic — it lives in our file.
"""
from __future__ import annotations

import os
from pathlib import Path


def attach(svc, a, cfg: dict, hub=None) -> None:
    """Build the HarnessManager beside the Service when --harness (or
    "harness": true in the config). The engine is reached in-process via
    make_service_engine (ADR-0002/0003): never HTTP to itself. `hub` is the
    same McpHub the Chat tab uses — one set of MCP processes per server
    (ADR-0012); None when no MCP servers are configured."""
    if not (getattr(a, "harness", False) or cfg.get("harness") is True
            or os.environ.get("STRATA_HARNESS") == "1"):
        # STRATA_HARNESS=1 is the container/quadlet opt-in: the engine config is
        # written by setup.py and the flag is a CLI arg; the env var keeps the
        # deployment decision in the unit file, not in either.
        return
    from .engine import make_service_engine
    from .manager import HarnessManager
    sessions_dir = Path(os.environ.get("STRATA_HARNESS_DIR",
                                       Path.home() / ".local/share/strata/harness")) / "sessions"
    # ADR-0014: self-authored plugins are OFF by default. Opt-in is a
    # server-start decision (env or run-config) — the browser can never turn
    # it on. When off, plugins_dir stays None: no scan, no import, no UI.
    plugins_on = (os.environ.get("STRATA_HARNESS_PLUGINS") == "1"
                  or cfg.get("harness_plugins") is True)
    plugins_dir = None
    if plugins_on:
        from .plugins import plugin_dir_default
        plugins_dir = Path(os.environ.get("STRATA_HARNESS_PLUGIN_DIR", plugin_dir_default()))
    svc.harness = HarnessManager(sessions_dir,
                                 lambda: make_service_engine(svc, sampling=dict(svc.sampling_defaults)),
                                 workspace_root=Path(os.environ.get("STRATA_HARNESS_WORKSPACE", os.getcwd())),
                                 # M9: operator default trusted folders for new
                                 # sessions (os.pathsep-joined); the quadlets
                                 # set this to /data/workspaces.
                                 default_roots=[r for r in os.environ.get("STRATA_HARNESS_ROOTS", "").split(os.pathsep) if r] or None,
                                 metrics_fn=lambda: svc.metrics(),
                                 mcp_hub=hub,
                                 plugins_dir=plugins_dir)
    print(f"[strata] harness on (/harness): agent sessions in {sessions_dir}; tools run on this PC behind "
          "approvals" + ("" if svc.api_key else "; no API key — anyone who can reach this page drives the agent")
          + (f"; plugins on ({plugins_dir}, hash-reviewed load only)" if plugins_dir else ""),
          flush=True)
