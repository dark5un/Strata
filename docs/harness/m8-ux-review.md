# M8 rendered UX review — plugins panel (ADR-0014)

Date: 2026-10-09. Method per docs/harness/viewport-matrix.md: five widths
(1920x1080, 1440x900, 1001x900, 999x900, 640x900), viewport set before
navigation, one js() call measuring getBoundingClientRect() of the popover,
trigger, rows and action buttons, then vision_analyze on the shots
(scratch/m8_panel_1920.png, m8_source_1920.png).

## Live scenarios verified in the rendered tab (DOM-asserted, not vision-read)

- plugins ON: toolbar button visible; OFF server: hidden (boot probe 404).
- Panel rows: greeter "active" (Source/Disable), unreviewed "needs review"
  (Source/Approve); warning dot + aria-label "1 plugin needs review" when
  anything is unreviewed; dot clears after approval.
- Approve = second-click-confirm ("Read source first?" -> approved -> both
  rows active, dot hidden).
- Edit-after-review: badge flips to "changed"; Source view shows the unified
  diff (2 add spans, header "--- greeter (reviewed) / +++ greeter (current)")
  plus the full source pre and the sha256/size meta line.
- yolo + write into the plugin dir: LOUD card "Write .../evil.py — 33 chars
  [plugin write]" + the two-gates note, while the yolo banner is on; the log
  records approval/decided {outcome: allow-once, asked: true} — the write
  gate held under yolo.
- yolo + write outside the dir: silent (decided {yolo, asked: false}).
- Plugin tool (greeter) under yolo: ran silently, fold row "greeter | yolo";
  under ask mode the card carries the `plugin` chip (unit-tested; the chip
  path shares the code the `plugin write` chip rendered live).

## Geometry (1920x1080, panel open)

popover 420px wide, right edge 1528 == trigger right edge 1528 (anchored),
gap to toolbar 59px, action buttons 13px from the panel edge, rows 42px
each, rowGap 8px. At 640px: panel right 616 < viewport 640, no overflow at
any width. Source view 640px wide, also no overflow.

## Vision findings vs DOM

Vision reported no BLOCKERs. Its SHOULDs about loose anchoring, a 4-6px
glued gap, and flush-to-edge buttons are REFUTED by the measurements above
(vision misjudged scale: it read the 420px panel as ~200px). Accepted and
applied:
- FIX (grammar): aria-label "1 plugin need review" -> "needs review"
  (harness.js pluginBadge).
- Noted, not changed: Source/Disable contrast and row rhythm match the
  existing preset-panel family (st-btn--secondary, 8px gap) — the design
  system bar the user set is consistency with the presets popover, and the
  DOM confirms identical geometry.

## Verdict

PASS. No BLOCKERs; one copy fix applied; layout claims verified by
measurement rather than impression.
