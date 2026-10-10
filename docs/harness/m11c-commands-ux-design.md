# M11c-commands — Slash commands: design pass

Date: 2026-10-10. Inputs: research/hermes-slash-commands.md (Tier-1
matrix), m11-parity-plan.md thread 3, ADR-0018 (skills store API).

## Semantics (Hermes shape, verbatim)

- Commands are handled by the harness (the tab), never reach the
  model: the composer parses `/name args` client-side and dispatches
  to existing routes/functions. The command text is never sent as a
  message.
- Prefix matching: `/he` -> `/help` on Tab/Enter when unique.
- Built-in names beat skill names (collision rule).
- Autocomplete popup on `/` at the composer start (reuse the
  wireRootsBrowse pattern): filtered list, name + one-liner.

## Command set (Tier 1, all mapped to what exists)

| command | maps to | notes |
|---|---|---|
| /new | + button path (POST session) | no confirm: sessions are durable, nothing is lost — differs from Hermes (its /new wipes the live context); documented in the ADR |
| /fork | Fork button path | as-is popover |
| /title <text> | NEW route: append session/title {by:"user"} | the fold already prefers the last SESSION_TITLE row |
| /roots | ws chip popover | opens it; args NOT accepted — roots are fixed per session (ADR-0015), the popover's fork path is the change story |
| /preset | Settings popover | opens it; `/preset <name>` sets the NEXT-session default (same as the select) |
| /compact | NEW route: manual compaction when idle | prune_tool_results + summarize_oldest; refuses while busy |
| /cancel | Stop button path | refuses when idle |
| /export [md\|jsonl] | export routes | arg picks the format, no popover; bare /export opens the popover |
| /status | client-side | sid, preset, roots, busy, ctx — data already in H |
| /context | client-side | the pill's numbers spelled out (meter fold, never re-derived) |
| /help | client-side | grouped command list + skill count line |
| /skills | GET /harness/skills | name: description list |
| /<skill-name> | user message "load skill '<name>' via skill_view" | the body is pulled by the MODEL (progressive disclosure intact); builtins win collisions |

Tier 2 (/steer, /queue) is deliberately NOT here: the composer
already steers automatically when busy (target:"steer") — the
explicit commands land when there is a UI affordance worth their
parser cost. /stop aliases /cancel (Hermes muscle memory).

## Command output rendering

Command replies are LOCAL transcript notes (`.harness-note`,
seq-less, ephemeral across reload — the log stays the session's
truth, ADR-0004: commands are not events). Errors ride the existing
toast. `/help` output is a note with the grouped list.

## States

- No session open: /new and /help work; everything else toasts
  "no session".
- Busy session: /cancel works; /compact and /new refuse with a
  one-line reason (not a silent no-op).
- Unknown `/word`: sent as a normal message ONLY if it doesn't start
  with `/`... it does — so: unknown slash word toasts "unknown
  command — /help" and sends nothing (a leading `/` is never a task).
  Escape hatch: a task that must start with `/` is typed with a
  leading space (trimmed only for command detection).

## A11y

- Autocomplete popup: role=listbox, aria-activedescendant on the
  input, Up/Down/Tab/Enter; Escape closes without swallowing the
  composer (first Escape closes the popup, second clears `/`).
- The popup is composer-anchored, not a bar popover (it is typing
  assistance, not a dialog — no scrim; ADR-0016 §1 governs dialogs).

## Copy

/help groups: Session (new fork title roots preset) · Run (cancel
compact export) · Info (status context skills help). Skill commands
collapse to one line: "+ N skill commands (/skills to list)".
