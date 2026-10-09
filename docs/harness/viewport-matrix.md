# Viewport matrix for rendered UX reviews

Research-based (2026-10-09). Sources: visual-regression practice guides
(frontendcomponent.com, argos-ci, tessl visual-baseline-conventions) +
StatCounter desktop resolution stats 2025-26.

## The rules we follow

1. Capture at the app's OWN breakpoints, not arbitrary device sizes — a
   layout only changes at a breakpoint; between them it stretches and a
   snapshot proves nothing its neighbour didn't.
2. Three widths is the floor, five comfortable; more only when a width is
   the only one showing a new arrangement. 12+ is an anti-pattern.
3. Capture immediately either side of each breakpoint — off-by-one media
   query bugs are invisible otherwise.
4. Pin height as deliberately as width (it decides what's above the fold);
   set the viewport BEFORE navigation (layout is computed at load);
   deviceScaleFactor 1.
5. Include the most common real desktop (1920x1080, ~22% share) and the
   tightest common laptop (1366x768) as stress cases.

## The matrix for Strata's UI

Strata's CSS has exactly two breakpoints: max-width 1000px (app.css) and
max-width 640px (app.css + harness.css). So:

| # | viewport | what it proves |
|---|---|---|
| 1 | 1920x1080 | wide desktop: max-width columns, gutters, balance |
| 2 | 1440x900  | everyday laptop |
| 3 | 1001x900  | just above the 1000px breakpoint: sidebar + 860px measure at tightest fit |
| 4 | 999x900   | just below it: breakpoint flip (app.css 1000px rules) |
| 5 | 640x900   | mobile collapse: harness stacks, sessions max-height 160px |

For the Harness tab specifically, #3 is the money shot: 240px sidebar +
gap + 860px measure = 1124px of content, so between 1124 and 1000 the
composer shrinks below its max-width — watch for text overflow there.

## How to run it (browser tool)

Resize the window to each viewport BEFORE goto, then per width:
- one js() call measuring getBoundingClientRect() of every control vs the
  Chat tab's equivalents (catches collapse/misalignment without pixels);
- one screenshot + vision_analyze (catches balance/affordance noise).
Strata runs VISION=cpu on all variants, so vision_analyze is always
available against 11434.
