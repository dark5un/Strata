# ADR-0009: agent presets are YAML (persona + tool allow/deny)

Date: 2026-10-09
Status: accepted

## Context

dsh's presets are YAML-declared per-agent compositions (tools/persona/
plugins) mounted at agent setup (research/dsh-agent-core.md §5), and tool
scoping is a `ToolRestriction {allow, deny}` intersected over the ancestor
chain. strata-harness needs the same for M4: subagents should run with a
narrower tool set + role prompt than the parent, without code changes.

## Decision

Presets are YAML files in `serve/harness/presets/*.yaml` (plus a built-in
`default` that is today's exact behaviour):

```yaml
name: researcher
persona: "You are a focused research subagent..."   # system section, ADR-0007 style
tools:
  allow: [read_file, search, spill_read]            # optional
  deny: [shell, write_file, edit_file]              # optional
```

- Loaded at attach time from the session header's `agentPreset` (already in
  the header since M1). Unknown preset name -> `default` + a warning event.
- `persona` rides the existing `system_sections` seam (ADR-0007): prompt
  injection only, no privilege change.
- `tools.allow/deny` filter the registry at attach; for subagents the
  restriction is **intersected with the parent's effective tool set**
  (dsh's rule): a child can never gain a tool its parent did not have.
- `delegate` gains a `deny` list (default: `delegate` itself) so children
  cannot fan out recursively beyond the budget.
- PyYAML is already a serve dependency (requirements.txt) — no new deps,
  no build step (ADR-0005 untouched).

## Consequences

- Presets are data, reviewable and upstreamable; adding one needs no Python.
- The preset select in the UI only applies to NEW sessions (persona+tools
  are session identity; mid-session change would fork instead).
- Malformed YAML fails closed: the session attaches with `default` and a
  durable note, never with an arbitrary tool set.
