# ADR-0018: Skills — a prompt-content store with progressive disclosure

Date: 2026-10-10
Status: accepted

## Context

dsh parity flagged skills as missing (parity-audit.md). The user
chose the Hermes shape: one dir per skill, SKILL.md with YAML
frontmatter, the system prompt carrying only name+description, the
model pulling bodies on demand (research/hermes-slash-commands.md,
m11-parity-plan.md thread 3). Design pass:
docs/harness/m11c-skills-ux-design.md.

## Decision

1. **Store location: `$STRATA_HARNESS_DIR/skills/`, outside the
   session fence.** The fence (ADR-0015) guards machine state the
   tools can touch; widening it to the harness state dir would expose
   session logs and spill. The agent reaches the store only through
   two dedicated built-ins, path-locked to it:
   `skill_view(name, file_path?)` (read) and `skill_write(name,
   content)` (create/replace SKILL.md). The m11-parity-plan's
   "skills/ is inside the fence" assumption is corrected here.

2. **Progressive disclosure via system_sections.** The "Available
   skills" index (name: description, one line each) is injected per
   turn — not snapshotted at attach. Editing a skill takes effect on
   the next turn. This deliberately differs from plugins' hash
   snapshot (ADR-0014): skills are prompt content, not code; there is
   no TOCTOU surface to freeze.

3. **No approval gate on skill_write.** Skills steer behaviour but
   execute nothing (Hermes posture: authoring is normal agent work).
   Guardrails instead: name regex + collision check, frontmatter
   must parse (name+description), 32 KB cap, the tool can write only
   SKILL.md inside the store, and the store is operator-visible.
   The self-modifying-prompt consequence (an agent-written skill
   steers future sessions) is the feature, not an accident.

4. **Preset gate: `skills:` allowlist in preset YAML** (ADR-0009
   shape). Absent = all visible; a list = only those names in the
   index, and skill_view on a hidden skill fails closed. Subagents
   intersect with the parent's visible set, same rule as tools.

5. **Malformed skills fail closed per skill**: skipped from the
   index with a one-line note; a bad file cannot break a session.

6. **Command hook point reserved**: the slash-commands pass will
   expose skills as `/name` commands against this same store API
   (list visible names, view body). No change needed here; plugin-
   registered skills stay out of scope.

## Consequences

- Context cost of the store when unused: ~30 tokens per skill.
- skill_view results are ordinary tool results: spillable, logged.
- The store is shared across sessions and presets gate visibility —
  a global store, per-preset selection (resolves the plan's open
  question the Hermes way).
