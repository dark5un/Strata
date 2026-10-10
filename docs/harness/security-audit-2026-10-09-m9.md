# strata-harness — security sweep (2026-10-09, post-M9)

Standing rule: re-run before any push, especially after M9 added
`harness/ls` (a filesystem-listing route = new surface).

## Result: PASS

### 1. Credential shapes — strata-harness
- `git grep` over HEAD for sk-/ghp_/AKIA/xox-style tokens and key
  assignments: zero hits outside upstream test fixtures (fake values,
  present in upstream history — same finding as the 2026-10-09 section-6
  audit, unchanged).
- Our 24 commits (upstream/main..HEAD): history sweep for the same
  patterns: zero matches.
- Our fork diff touches no .env/secret/key file names.

### 2. Credential shapes — ai-lab-quadlets
- Tracked files: zero credential-shaped hits. Only `*.env.example`
  templates are tracked (placeholders); the real
  `~/.config/containers/config/strata/service.env` lives outside the repo
  and was never tracked.

### 3. Sensitive paths never tracked
- No session logs (`~/.local/share/strata/harness/sessions/*.jsonl`),
  plugin dirs, or service.env in git. The tracked `bench/**/*.jsonl` are
  upstream-owned benchmark telemetry (no commits of ours touch bench/).
- `.gitignore` covers `.prowl/` and `scratch/` (verified with
  `git check-ignore` on specific files, not just dir names).

### 4. harness/ls review (the new route)
- Directories ONLY: a non-directory path is refused with 400 (test
  `test_ls_lists_directories_only` asserts a file path discloses nothing
  and file names inside a dir never appear in the response).
- Hidden dirs (dot-prefixed) and system dirs (proc/sys/dev/run/tmp) are
  skipped; 500-entry cap per call (no whole-filesystem slurp).
- Gated by the same api-key + `_authorized()` check as every /harness
  route (the route sits behind it in `handle_get`); no unauthenticated
  listing exists.
- Threat model: directory NAMES are disclosed only to an api-key-holder —
  the same principal that can already drive the agent and run `ls`
  through an approved shell call. The route adds no new trust level; it
  removes no approval gate. The per-session fence is unaffected: ls is
  picker UX, not a tool the agent can call.

### 5. Gates at the sweep point
- tools/test_harness.sh: ALL GREEN (195 harness tests + ruff + pyright + eslint).
- Full serve suite: 788 tests, exactly the 2 known environmental failures
  (test_vision_device GPU order on this box; sampler teardown flake).

Push status: NOT pushed — user gate (standing rule: sweep precedes push;
the push itself waits for the user).
