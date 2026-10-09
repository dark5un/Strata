"""serve/harness/cli.py — headless driver: prove the loop end-to-end from a terminal.

    python -m serve.harness.cli --base-url http://127.0.0.1:11434/v1 --model qwen3.8-flash-next-iq3_s

The API key is read from the environment (STRATA_API_KEY) or the run-config
file — never from argv (it would land in shell history). Commands:
  /new /resume <id> /list /fork <id> /policy ask|never|allowlist[:pat] /quit
anything else is a user message. Approvals prompt on the terminal.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from .engine import make_http_engine
from .manager import HarnessManager


def _api_key(args) -> str | None:
    if args.api_key_env:
        return os.environ.get(args.api_key_env)
    cfg = Path.home() / ".local/share/strata/config-5090-1m/strata-iq3_s.json"
    if cfg.exists():
        import json
        return json.loads(cfg.read_text()).get("api_key")
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="strata-harness-cli")
    ap.add_argument("--base-url", default="http://127.0.0.1:11434/v1")
    ap.add_argument("--model", default="qwen3.8-flash-next-iq3_s")
    ap.add_argument("--api-key-env", default="STRATA_API_KEY")
    ap.add_argument("--sessions", default=str(Path.home() / ".local/share/strata/harness/sessions"))
    ap.add_argument("--workspace", default=os.getcwd())
    ap.add_argument("--once", help="send one message, print the answer, exit")
    args = ap.parse_args(argv)

    key = _api_key(args)
    sessions = Path(args.sessions)

    def factory():
        return make_http_engine(args.base_url, args.model, api_key=key)

    mgr = HarnessManager(sessions, factory, workspace_root=Path(args.workspace))
    sid = mgr.new()
    print(f"session {sid}  (workspace {args.workspace})")

    def wait_done():
        while mgr.live(sid).busy:
            time.sleep(0.05)

    def pump():
        """Drain approvals/questions and print events as they land."""
        live = mgr.live(sid)
        while True:
            try:
                ask = live.approvals.get_nowait()
            except Exception:
                ask = None
            if ask is not None:
                ans = input(f"\napprove {ask['tool']} [{ask['preview'][:80]}]? (y/n) ").strip().lower()
                mgr.decide(sid, ask["call_id"], "allow-once" if ans == "y" else "deny")
            q = live.board.pending()
            if q:
                question = q[0]
                ans = input(f"\nmodel asks: {question.question} ").strip()
                mgr.answer(sid, question.qid, {"text": ans})
            if not live.busy:
                return
            time.sleep(0.1)

    def show():
        live = mgr.live(sid)
        rows = live.handle.snapshot()
        for row in rows:
            t, d = row["type"], row["data"]
            if t == "assistant/message" and d.get("content"):
                print(f"\nmodel: {d['content']}")
            elif t == "tool/call":
                print(f"  -> {d['name']} {str(d['arguments'])[:100]}")
            elif t == "tool/result":
                print(f"  <- {d['text'][:200]}")
            elif t == "turn/end":
                print(f"  [turn {d['reason']}]")

    if args.once:
        mgr.send(sid, args.once)
        pump()
        wait_done()
        show()
        return 0

    print("type a message; /list /resume <id> /fork <id> /policy ask|never /quit")
    while True:
        try:
            line = input("\nyou: ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if not line:
            continue
        if line == "/quit":
            return 0
        if line == "/list":
            for s in mgr.list_sessions():
                print(s)
            continue
        if line.startswith("/resume "):
            sid = line.split(None, 1)[1].strip()
            mgr.live(sid)
            print(f"resumed {sid}")
            continue
        if line.startswith("/fork "):
            sid = mgr.fork(line.split(None, 1)[1].strip())
            print(f"forked into {sid}")
            continue
        if line.startswith("/policy "):
            spec = line.split(None, 1)[1].strip()
            mode, _, pats = spec.partition(":")
            mgr.set_policy(sid, mode, [p for p in pats.split(",") if p])
            print(f"policy: {mode} {pats}")
            continue
        mgr.send(sid, line)
        pump()
        wait_done()
        show()


if __name__ == "__main__":
    sys.exit(main())
