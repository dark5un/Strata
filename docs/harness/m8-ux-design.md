# M8 UX design pass — self-authored plugins (ADR-0014)

Status: design (pre-implementation). Driven by the user's decision (2026-10-09,
"go for 2") to enable the deferred M7c-3 plugin surface, with the guardrails
the plan already spelled out.

## The threat model this UI must make legible

A self-authored plugin is arbitrary Python that runs inside the server on
every future start. The danger the user accepted is exactly: an agent (or a
prompt injection) writes a plugin file with one approved write_file, and from
then on it is permanent code execution with no approval card. The UI's job is
to make the two gates visible and honest:

1. OFF BY DEFAULT. No plugin dir scan, no import, no UI affordance beyond a
   one-line note, until the user opts in (env STRATA_HARNESS_PLUGINS=1 or
   run-config "harness_plugins": true). Opt-in is a server-start decision,
   not a browser decision — the tab cannot turn it on.
2. LOAD-TIME REVIEW. A plugin file is imported ONLY when its sha256 matches
   the reviewed manifest (<plugin dir>/.reviewed.json). New or changed files
   are QUARANTINED: listed, never imported, until a human reads the source
   and approves. Approval is per (name, hash): editing a plugin re-quarantines
   it.
3. WRITES INTO THE PLUGIN DIR ARE NEVER ALLOWLISTABLE. write_file/edit_file
   targeting the plugin dir force the human ask path even under allowlist or
   yolo policy — the card is the only way through. The card must say so
   ("this writes a plugin — it will need review before it loads").

## D1 — Where the surface lives (placement decision)

Plugins are SERVER-scoped state (like MCP servers), not session state. But
the user's mental model is the tab. Decision:

- Toolbar: a "Plugins" button next to "Manage presets" (same popover family,
  harness-preset-pop styling reused), with a warning dot when anything is
  quarantined or failed. The button is hidden entirely when plugins are off
  (opt-in), so the default UI is unchanged.
- No transcript cards for plugin state: the transcript is per-session and
  plugin review is not a session event. The exception is gate 3: a write into
  the plugin dir IS a session event — it rides the existing approval card
  (path hero line, full content expandable), plus a loud badge chip
  "plugin write" on that card.

## D2 — The plugins panel (states)

Rows, one per .py file found (sorted):

| state | badge | actions | what it means |
|---|---|---|---|
| active | `active` (green reading badge) | Disable, View source | hash matches manifest, imported at last attach |
| quarantined | `needs review` (warning) | Approve, View source | NOT imported; tools/hooks absent from every session |
| changed | `changed` (warning) | Approve, View source | was reviewed, hash moved; the OLD one stays active until reviewed? NO — fail closed: a changed file is quarantined immediately at next attach; the panel says "was active, now needs review" |
| failed | `error` | View source, retry note | import/register raised; error text inline, truncated with expand |

Copy rules:
- Approve is second-click-confirm (same pattern as Delete preset).
- Approve tooltip states the consequence: "imports this code into every new
  session on this server".
- Empty state: "No plugins. Drop .py files into <dir> — they load only after
  you review them here." (the dir path is the honest answer to "where?")
- Footer note: "Plugins are code. Review the source before approving.
  Writes to this folder always ask, even in yolo."

## D3 — The review view (the diff requirement)

"Load-time review card diffing since last boot" — the panel's View source
shows:
- the full file text in a scrollable pre (same code-card style as tool
  previews; reuse the Chat tab's code rendering, no new primitives);
- if a previous reviewed hash exists for the same name: a unified diff
  (difflib on the server, GET /harness/plugins?name= returns {source,
  prev_source}) rendered with +/- lines tinted via existing token colours.
- header line: name, sha256 (short), size, mtime.

This is the only place in the tab that shows raw source; it is gated by the
same _own_page + api-key rules as every other /harness route.

## D4 — The plugin-write approval card (gate 3)

When a session's write_file/edit_file targets the plugin dir:
- the card gets a `plugin write` chip next to the tool name (same badge
  family as the `mcp` chip from M7c);
- the card title: "Write <path>? — plugin write" and the body note: "This
  file becomes server code after you review it in the Plugins panel. It will
  NOT load automatically." — the two gates are independent and the copy says
  so;
- under yolo/allowlist the ask still happens (forced), and the banner text
  explains why the loud card appeared despite yolo.

## D5 — a11y + design-system mapping

- Panel: role="dialog", aria-label "Manage plugins", Escape closes and
  returns focus to the toolbar button (mirror of the preset panel).
- Badges reuse st-badge variants; warning state uses the existing
  generating/amber token, not a new colour.
- Approve/Disable are st-btn--secondary; the confirm state changes text only
  (no new component), same as Delete preset.
- The warning dot on the toolbar button is aria-hidden; the button's
  aria-label gains "N plugins need review" when quarantined>0.

## D6 — What is deliberately NOT in this milestone

- No plugin CRUD UI (the filesystem is the editor; the panel reviews, it does
  not author). Same rule as presets: file is the source of truth.
- No hot-reload mid-session: the tool/hook set is snapshotted at attach
  (ADR-0012 precedent — keeps request/header honest).
- No per-session plugin toggles; no preset allow/deny bypass for plugin tools
  (they are namespaced names, so ADR-0009 vocabulary already covers them).
- No signing/allowlist-by-path beyond the hash manifest.

## Acceptance (for the rendered review)

- Opt-in off: tab identical to M7c (no button).
- Opt-in on, empty dir: button visible, panel empty state names the dir.
- Quarantined file: warning dot, row badge, Approve confirm flow, source view.
- Changed file: re-quarantined, diff shown vs previous reviewed source.
- yolo session + write into plugin dir: loud card with chip + explanatory
  note; write elsewhere: silent yolo as before.
- Viewport matrix per docs/harness/viewport-matrix.md; panel fits the
  toolbar measure at all five widths.
