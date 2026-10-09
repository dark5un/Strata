"""serve/harness — the strata-harness agent loop, sessions and tools.

A DeepSeek-Harness-influenced agent runtime living inside Strata's serve layer.
Design decisions: docs/harness/adr/. Rules that hold across the package:

  * stdlib only (ADR-0003/0005): threading, json, subprocess, pathlib.
  * the session log is the truth (ADR-0004): append-only JSONL of typed
    events; LLM history is derived from the surface projection, never stored.
  * the engine is reached through the in-process Service and its monitor
    JSON only (ADR-0002) — never through C++ or a loopback HTTP hop.
  * every module ships a unittest file next to it; tests run with MockEngine,
    no GPU: python -m unittest discover -s serve/harness -t .
"""

HARNESS_FORMAT_VERSION = 2   # v2: request/header carries the full envelope (M7b)
