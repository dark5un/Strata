# M11c — Slash commands + skills: rendered UX review

Date: 2026-10-10. Method per viewport-matrix.md: DOM assertions for
text, vision for layout only. Mock server :8199.

## Functional DOM assertions (all PASS)

- /help renders the grouped list (Session/Run/Info) + skill-command
  count line as a transcript note.
- Autocomplete: "/" opens 12 items above the composer; typing "/tit"
  filters to 1; Tab completes to "/title " (unique-match completion);
  Enter with nothing selected runs the typed text.
- /title renamed the session end-to-end: sidebar row shows
  "renamed by command" (server fold, survives reload).
- Unknown /nonsense -> toast "Unknown command — /help lists the
  commands"; nothing sent to the model.
- /compact on a tiny session -> 409 with the reason as a note
  ("nothing to compact..."); double-prefix copy bug found and fixed.
- /skills lists the store; /<skill-name> posts the load-skill message
  (model pulls the body — progressive disclosure intact).
- Session-switch/reload: command notes are ephemeral (log stays the
  session's truth, ADR-0004).

## Viewport matrix (autocomplete popup open, getBoundingClientRect)

| width | popup | notes |
|---|---|---|
| 1920x1080 | 260px tall, composer-width, above composer | no overlap with input or bar |
| 1440x900  | same | |
| 999x900   | same | below the 1000px breakpoint |
| 640x900   | 590px wide (composer-width), fits | |

Never off-screen, never covers the bar buttons or the input line.

## Vision verdicts

- Readable, evenly spaced, does not crowd the controls. SHOULD
  applied: description column was ragged (each hint started after
  its own command name) -> `strong { min-width: 7.5em }` aligns the
  hints into a column.
- Claimed "right-edge misalignment": contradicted by the geometry
  probe (left:0/right:0 = exactly composer width at every width) —
  vision misread, not fixed.
- Claimed "weak elevation": the popup carries the same --st-surface-2
  fill + deep shadow as every popover; on a near-black theme the
  shadow is invisible by physics (ADR-0016 §1 pixel lesson). Changing
  it here would break uniformity. Deliberate.

## BLOCKERs: none.
