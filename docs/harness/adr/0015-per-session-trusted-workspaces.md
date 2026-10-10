# ADR-0015: per-session trusted workspaces

Date: 2026-10-09
Status: accepted

## Context

`HarnessManager` had ONE `workspace_root` for all sessions, defaulting to the
server's cwd (inside the container: the app dir). The user asked (2026-10-09)
for sessions to carry their own list of trusted folders — "add folders we
trust the session to work on", subfolders included — so an agent can be
pointed at real project directories without widening the whole server's
surface. Design settled in `~/Documents/plans/strata-harness/m9-workspaces-design.md`:
folders are just folders (no harness-side volume namespace — mounted-or-not is
the quadlets' decision), roots lock at session creation, children may narrow
but never widen them.

## Decision

1. **Header field, additive.** A session's roots ride its `session/header`
   row as `trustedRoots: [...]` — durable, per-session, same place the
   session itself lives. Read-compat: an old header folds to `[header.cwd]`;
   a header with no cwd folds to `None` = the operator's full-surface
   posture (today's `root=None`, unchanged). No format bump, no migration.
2. **Multi-root fence.** `tools/fs.py::_resolve` accepts one root, a list,
   or None: a path is inside the fence if it resolves inside ANY root (or
   equals one), symlinks resolved before the check. The escape error names
   every root so the agent learns the boundary from the tool error.
   `read_file` now goes through `_resolve` (it previously bypassed the fence
   with its own inline join — closed here). `shell` cwd defaults to the
   session's FIRST root; an explicit `cwd` argument is fenced like any path.
   `search` path is fenced.
3. **Lock at creation; forks narrow.** Roots are fixed when the session is
   created (the header is written once); a live session's roots are
   read-only in the UI ("fork to change"). `fork(roots=...)` accepts only a
   subset of the parent's resolved set — widening raises. Subagents inherit
   the parent's roots at spawn (ADR-0008 precedent: children never exceed
   the parent's surface).
4. **Validation at creation.** Each root must exist and be a directory
   (`manager.new(roots=...)`): a typo'd root would silently deny everything.
5. **The picker browses the container's real filesystem** via a new
   `GET harness/ls?path=` route: directories ONLY (a file path is refused
   with 400; file names are never disclosed), hidden and system dirs
   skipped, 500-entry cap, API-key gated like every other /harness route.
   This is new read surface — the security sweep reviewed it specifically.
6. **UI (m9 design pass):** session-bar chip (short name + `+N`), roots
   popover with autocomplete, session-list sub-badge when roots != default,
   approval cards show which root a call lives in ("inside X" / "outside
   every trusted folder — will be refused"). Icon-only chip below 720px.

## Consequences

- Sessions can be pointed at real project dirs without touching the
  server-wide default; the outer boundary stays the container's mount set
  (ai-lab-quadlets' domain — a `Volume=` for a shared workspaces tree is a
  separate, user-gated decision, not part of this repo).
- The fence is per-session from the header at attach: restart-safe, and a
  fork/subagent can never escape the parent's sandbox.
- `harness/ls` discloses the container's directory NAMES to an
  api-key-holder (same trust level as the agent itself, which can already
  `ls` through an approved shell call); it discloses nothing to anyone else.
- Old session logs keep working untouched.
