# M11c-skills — Skills store + progressive disclosure: design pass

Date: 2026-10-10. Inputs: m11-parity-plan.md thread 3, parity-audit.md
(skills row), research/hermes-slash-commands.md. Slash commands are a
separate pass (m11c-commands-ux-design.md) — this doc covers the
skills store, the agent tools, and the preset gate.

## What a skill is (Hermes format, verbatim where possible)

One directory per skill under the server's skill store:

```
$STRATA_HARNESS_DIR/skills/<name>/SKILL.md
```

SKILL.md = YAML frontmatter (`name`, `description`) + markdown body.
The description is the trigger line: it is the ONLY thing the model
sees until it asks for the body. Supporting files (references/,
templates/, scripts/, assets/) are readable by path through the same
tool. No code executes — a skill is prompt content (contrast with
plugins, ADR-0014: reviewed code).

## Progressive disclosure (the load-bearing property)

- Per turn, the system prompt carries an "Available skills" block:
  one line per visible skill — `name: description`. That is the whole
  context cost of the store when unused (~30 tokens/skill).
- The model pulls a body with the `skill_view` tool (name -> full
  SKILL.md; optional file_path -> a supporting file). Read-only,
  bounded (size cap), path-sanitized (no traversal out of the skill's
  own dir).
- The index is injected per turn (system_sections), NOT snapshotted
  at attach: editing a skill takes effect on the next turn, no fork
  needed. This is deliberate and differs from plugins' hash-snapshot
  (prompt content vs code — the ADR states why).

## The store lives OUTSIDE the session fence — and that is a correction

The parity plan assumed "skills/ is inside the fence, the agent can
write_file into it". It is not: the deployed fence is
/data/workspaces; the store is /data/harness/skills. Widening every
session's fence to cover the store would expose the harness state dir
(sessions, spill) to every file tool. Instead: two dedicated
built-in tools, path-locked to the store:

- `skill_view(name, file_path?)` — read SKILL.md or a supporting
  file. needs_approval=False (read-only, like plan/goal tools).
- `skill_write(name, content)` — create/replace SKILL.md. Validates:
  name `[a-z0-9][a-z0-9_-]{0,63}`, content must parse as frontmatter
  with name+description, size cap 32 KB. needs_approval=False —
  skills steer behaviour but execute nothing (Hermes posture: skill
  authoring is normal agent work). The write lands in the store,
  never in the session's roots; the tool cannot touch anything else.

Self-modifying-prompt note for the ADR: a skill the agent writes
steers its FUTURE sessions. That is the feature (Hermes does exactly
this); the guardrail is that skills are prompt-only, size-capped, and
the store is visible to the operator (and listed by GET /harness/skills).

## Preset gate (ADR-0009 shape)

Preset YAML gains `skills:` — an allowlist of skill names.
- absent/null: every skill in the store is visible.
- list: only those names appear in the index; skill_view on a hidden
  skill returns "not available in this session" (fail closed, same
  vocabulary as tools).
Subagent intersection: a child sees the intersection of its preset's
skills and its parent's visible set (same rule as tools, ADR-0009).

## UI surface (small, uniform)

- Preset editor: one labeled text input `skills` (comma-separated
  names, placeholder "leave empty for all"), mirroring the approvals
  allowlist input pattern. No new panel, no new popover.
- GET /harness/skills: [{name, description, files}] — the editor's
  placeholder hint + future command autocomplete source.
- No Skills button in the bar this milestone: the store is read by
  the model, edited by the operator (files) or the agent
  (skill_write). A browse panel is deferred until there is an
  operator-side edit story.

## Seed skills (operator-authored, shipped in the quadlet volume)

Three seeds, the "basics relative to our work" from the plan:
- strata-ops: GPU slot rules, ai-lab quadlets, resource gate, ports.
- harness-dev: repo layout, test gates, ADR conventions.
- rashin: pointer to the system vault + prowl commands.
They double as the format test: if SKILL.md conventions can't carry
them, the format gets a tweak before self-authoring is advertised.

## States & errors

- Store dir missing/empty: no "Available skills" block at all
  (silence at defaults), skill_view -> "no skills installed".
- Malformed SKILL.md: skipped from the index with a one-line warning
  appended to the block ("skill 'x': missing description") — fail
  closed per skill, never kills the session (preset warning pattern).
- skill_view unknown name: tool result error naming the visible set.

## A11y / a12y (agent-legibility)

The index line format is fixed: `- <name>: <description>` — the
model addresses skills by exact name; names are case-sensitive and
collision-checked at write (a new skill cannot shadow an existing
one with a different case).

## Non-goals (M11c-skills)

- No skill search/embeddings, no bundles, no /learn distiller.
- No skill CRUD UI (operator edits files; agent uses skill_write).
- No plugin-registered skills (hook point named in the ADR, out of
  scope).
- Slash-command invocation of skills (`/name`) lands with the
  commands pass — the store API is designed so it needs no change.
