# ADR-0011: user-editable presets in a user dir; the YAML file stays the source of truth

Date: 2026-10-09
Status: accepted

## Context

Presets (ADR-0009) load only from the bundled `serve/harness/presets/`
dir inside the package. The user asked for "a way to make more" — bundled
presets are not editable at all, and there is no UI path to author one.

## Decision

- Two directories, user wins on name collision:
  1. user dir: `~/.local/share/strata/harness/presets/` (overridable with
     `STRATA_HARNESS_PRESET_DIR`; `HarnessManager(presets_dir=...)` still
     points tests at a temp dir),
  2. bundled dir: `serve/harness/presets/` (read-only by contract).
- `load_preset` resolves user dir first, then bundled. A malformed file
  fails closed exactly as before (default + warning event).
- `list_presets` merges both dirs and tags each entry `{source:
  "user"|"bundled"}`.
- CRUD routes: `GET /harness/presets` (list, extended), `GET
  /harness/presets?name=` (read one, for the editor), `POST
  /harness/presets` (create/update in the USER dir), `DELETE
  /harness/presets?name=` (user dir only). Names are validated:
  `[A-Za-z0-9._-]{1,64}`, never `default`, no path separators.
- The POST body is the schema (name, persona, tools.allow, tools.deny,
  description); the server serializes YAML with the same safe subset the
  loader accepts. The file is the truth; the tab's manage panel is an
  editor, not a database.
- Editing a bundled preset from the UI duplicates it to the user dir first
  (the client asks for the source text via GET, then POSTs under the same
  name — the user copy shadows the bundled one).
- Preset changes apply to NEW sessions; a running session changes preset
  via fork (unchanged rule from ADR-0009).

## Consequences

- Presets remain data, reviewable on disk, upstreamable; no new deps
  (PyYAML already allowed for presets by test_scaffold's contract).
- A user preset can grant nothing beyond the registry: allow/deny still
  only filters existing tools, and subagent intersection (ADR-0009) is
  untouched.
- Deleting a user preset that shadows a bundled one re-exposes the bundled
  version — expected and shown by the badge.
