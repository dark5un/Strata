# M5 — upstream PR series plan (slice mapping)

Companion to rfc-upstream.md (published as Niko1221/Strata#1719). Maps the
12 commits on `harness` (upstream base 6674a00 / 0.1.40.4) to the five PR
slices proposed in the RFC. Branches are cut locally off `upstream/main`;
pushing needs user approval (origin remote = new network surface).

## Slice branches (target names)

| branch | contents | source commits |
|---|---|---|
| `pr/1-scaffold` | ruff.toml, pyrightconfig.json, eslint.config.mjs, tools/test_harness.sh, docs/harness/adr/0001-0005, serve/harness/__init__.py skeleton + smoke test, `--harness` flag + routes-table tail + hidden tab button in server.py/index.html | split of 0588c86 (M0 part) |
| `pr/2-core-loop` | events/session/surface/loop/manager/approval/engine/cli + tools/ + test_* for them (policies, tools, engine adapters, live-session manager) | rest of 0588c86, d96c096, e9d1790, 96bcb5a |
| `pr/3-tab` | serve/web/harness.js/css, index.html tab markup, routes.py SSE + commands, monitor gauge; M2 UX review fixes folded in | e6df7af + efc20aa |
| `pr/4-context` | meter/compaction/spill/inbox/modes + tools/modes,tools/spill,tools/todo, ADR-0006/0007, M3 fixes + UX review | b1d85ad + 9e79760 + 170b38a |
| `pr/5-subagents` | subagents.py, presets.py + presets/, export.py, keyboard nav, ADR-0008/0009, M4 UX review fixes | 3ff2910 + bf52062 |

## Cutting rules

- 0588c86 must be split (scaffold vs core): `git checkout -b pr/1-scaffold upstream/main` then
  `git checkout 0588c86 -- <scaffold paths>` + the flag/tab hooks from server.py/index.html
  (hunk-level: use `git show 0588c86 -- serve/server.py serve/web/index.html | git apply --cached`
  and stage only the additive hook hunks), commit, then build pr/2 on top of pr/1 with the
  remaining files.
- Later slices stack: pr/2 off pr/1, pr/3 off pr/2, etc. Each branch must pass
  `./tools/test_harness.sh` at its own tip (scaffold smoke test only on pr/1).
- Docs (ADRs, UX design/review records) ride in the slice that introduced them.
- Rebase onto current upstream/main before pushing (Strata moves weekly);
  additive cut points (routes table tail, tab list) should rebase cleanly.

## Gate before any push

1. User approval for origin remote + PRs (standing rule).
2. Security audit is PASS as of 2026-10-09 (security-audit-2026-10-09.md);
   re-run the sweep if anything lands between now and push.
3. Check #1719 for maintainer feedback first — the series shape may change.
