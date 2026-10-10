# M9 UX review — per-session trusted workspaces (rendered pass, 2026-10-09)

Method (docs/harness/viewport-matrix.md): viewport set BEFORE navigation,
five widths (1920x1080, 1440x900, 1001x900, 999x900, 640x900 — the app's
breakpoints are 1000px and 640px), one js() call measuring
getBoundingClientRect() of the chip vs the bar/policy/preset controls, then
vision_analyze on the screenshots for layout/balance only (DOM asserted for
text — vision garbles small UI text).

## Measurements (chip vs the session bar)

| width | chip rect | in bar | fits viewport | label |
|-------|-----------|--------|---------------|-------|
| 1920  | 143x42 @ (869,118) | yes | yes | shown |
| 1440  | 143x42 @ (629,118) | yes | yes | shown |
| 1001  | 143x42 @ (318,118) | yes | yes | shown |
| 999   | 143x42 @ (316,118) | yes | yes | shown |
| 640   | 103x42 @ (513,306) | yes | yes | hidden (icon-only, per design) |

The chip sits left of the approvals select at every width (design D1); the
720px icon-only rule fires at 640 (label display:none, chip shrinks 143->103).
No control overlaps another at any width.

## Live flow verified (mock server, DOM-asserted)

- Picker: typing a path fragment autocompletes from harness/ls (one hit for
  "…/pro" -> the proj dir); Enter adds; chip updates to "other +1".
- New session: POST carries roots; the session-list row gets the sub-badge
  ("other +1"); the chip shows the session's roots.
- Durability: full page reload -> chip restores both roots from the session
  header (server-side), not browser state.
- Approval card: the shell card renders; the tool card folds to its quiet
  row when done (ctx-pill fix ride-along).

## Findings

BLOCKER: none.

SHOULD (applied): roots rows in the popover ellipsize long paths — added a
title attribute so the full path is reachable on hover (the ellipsis is
deliberate: two 50-char paths must not wrap the popover at 640px).

NOTED (not fixed, pre-existing): at 640px the bar's control groups wrap into
three right-aligned rows with ragged left edges (the chip adds one row to an
already-wrapping toolbar). This is the Chat-tab bar's existing wrap behavior;
the chip survives it (measured above). Fixing the bar's wrap grid is an
upstream-UI decision (user rule: style over, don't restructure).

A11y: chip is a button with aria-haspopup=dialog + aria-expanded; popover is
role=dialog with an aria-label; the roots list is a ul with per-item remove
buttons (aria-label names the path); the suggest list is role=listbox with
ArrowDown/ArrowUp navigation and Escape returning focus to the chip.
