# ADR-0014: self-authored plugins (opt-in, hash-gated review)

Date: 2026-10-09
Status: accepted

## Context

PLAN M7c-3 deferred self-authored plugins: "a self-written plugin is
arbitrary Python that auto-loads on every future start: one approved
write_file becomes permanent code execution with no approval card." The user
decided (2026-10-09) to enable it, with the guardrails the plan spelled out.
The loop already has the hook surface (ADR-0013) and the registry accepts
programmatic tools (ADR-0012); what is missing is a safe loading story.

## Decision

1. **Opt-in, off by default.** Plugins load only when
   `STRATA_HARNESS_PLUGINS=1` (env) or `"harness_plugins": true` (run-config)
   is set at server start. Without it: no directory scan, no import, no UI
   affordance. The browser can never turn plugins on.
2. **The plugin dir** is `~/.local/share/strata/harness/plugins/`
   (overridable via `STRATA_HARNESS_PLUGIN_DIR`). One `.py` file = one plugin.
   The filesystem is the editor: no CRUD UI (presets precedent, ADR-0011).
3. **Hash-gated review.** A plugin is imported ONLY if its sha256 matches the
   reviewed manifest `<plugin dir>/.reviewed.json` (`{name: {sha256}}`).
   New or changed files are QUARANTINED: listed in the panel, never imported.
   Approval is per (name, hash): editing a plugin re-quarantines it at the
   next attach. Reviewing happens in the tab's Plugins panel (source view +
   diff vs the previously reviewed hash), gated by the same api-key +
   _own_page rules as every /harness route.
4. **Writes into the plugin dir are never allowlistable.** `write_file` /
   `edit_file` targeting the plugin dir force the human-ask path even under
   `allowlist` or `yolo` policy (approval.py: a `protected_dirs` check in
   `decide()` returns "ask" before any pattern match). The card carries a
   `plugin write` chip and copy explaining the second gate (review before
   load). This closes the "one approved write_file = permanent code" hole:
   the write always asks, and the write alone never activates anything.
5. **The plugin contract** (module-level, all optional):
   `name`, `description`, `tools() -> list[ToolDefinition]`,
   `hooks() -> dict[str, list[Callable]]` (keys pre_step/pre_tool/post_tool,
   ADR-0013 signatures). A plugin that registers tools gets them as ordinary
   registry entries — namespaced `<plugin>_<tool>` is NOT enforced (the
   plugin's own names ride preset allow/deny and subagent intersection like
   any tool), but a collision with an existing tool name is a hard register
   error: the plugin fails, it never shadows a built-in.
6. **Snapshotted at attach** (ADR-0012 precedent): the tool/hook set is
   fixed per session; a review takes effect for NEW sessions and forks, not
   mid-session. Keeps request/header honest.
7. **Failure isolation**: import or contract errors mark the plugin `failed`
   in the panel (error text inline) and skip it; the server and other
   plugins continue. No sandboxing beyond this — a reviewed plugin runs with
   the server's own privileges, and the ADR says so out loud.

## Consequences

- Two independent gates (write-ask + load-review): neither alone is bypassed
  by yolo or by an allowlist pattern; both must be passed deliberately.
- The manifest is data next to the code; deleting `.reviewed.json`
  quarantines everything (a "revoke all" escape hatch).
- New durable vocabulary: none (no format bump — plugin state is
  server-scoped, not session-logged; the forced ask reuses approval/asked).
- Routes: GET /harness/plugins (list+state), GET /harness/plugins/source
  (text + prev reviewed text), POST harness/plugins/review {name, sha256}
  (approve; second gate is the human reading the diff), POST
  harness/plugins/disable {name} (drop from the manifest -> quarantine).
- Self-authored plugins remain the riskiest surface in the project: the
  review UI must show FULL source and a diff, never a truncated preview
  (M7a rule: never approve what you cannot read).
