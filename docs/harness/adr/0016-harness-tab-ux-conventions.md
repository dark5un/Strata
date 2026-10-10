# ADR-0016: Harness tab UX conventions

Date: 2026-10-10
Status: accepted

## Context

The M9/M10 screenshot reviews (user, acting as design lead) converged on
recurring verdicts: dialogs over dialogs must show which one is live;
hiding controls is not tidiness; the bar felt crowded; icon-only
affordances read as decoration. The fixes landed in commits 5de42a9,
ebef32, 956da81, 0888cad. This ADR freezes the conventions so future
harness surfaces follow them instead of re-debating them.

## Decisions

### 1. Dialog focus is shown, not implied (scrim stack)

Every popover mounts through `mountPopover()`: one scrim per popover,
stack depth sets z-index, a dialog opened over a dialog dims the dialog
below it. Scrim click and Escape close exactly ONE popover (the top);
the synthetic-Escape path is flag-guarded so it never cascades.

Dark-theme rule: a black scrim over a near-black theme is invisible
(pixel-verified). The dim must RAISE the tone — `rgba(236,238,242,.12)`
+ 1.5px backdrop blur. Popovers lift via `--st-surface-2` fill,
brighter edge, deep shadow. Verified by pixel diff (background +25 gray
levels, detail std down) and vision review, not by eye alone.

### 2. Uniformity is queen

Every bar action is a plain `st-btn--secondary` text button
(Settings · Export · Plan · Goal · Fork). No icon-only buttons in the
bar, no emphasis hierarchy between actions. State rides as a badge ONLY
when it deviates from the default (Settings shows a badge for
yolo/allowlist/non-default preset; silence otherwise — same pattern as
the ws chip's +N). Full current state lives in the title attribute.

### 3. Hiding is not tidying

Crowding relief may move controls into a popover (the Settings
cluster), but the popover must show the FULL surface, labeled —
nothing behind icon-only affordances ("Manage presets…" as a text
button, not a gear). If a control is important enough to exist, it is
important enough to be labeled.

### 4. Destructive actions confirm in place

Session delete: trash revealed on row hover/focus, two-step inline
Delete/Keep (no native confirm()). Confirming rows hide badges/icons so
the verdict fits the 240px sidebar. The JSONL log IS the session
(ADR-0004), so delete is final — the confirm is the safety, matching
Hermes' destructive-slash convention.

### 5. Escape closes dialogs, always

Every popover binds its Escape path on the popover NODE (not an inner
input), so global/scrim Escape reaches it. Focus position (trigger,
inside, body) must not change the outcome.

## Consequences

- New popovers must use mountPopover/unmountPopover; hand-rolled
  appendChild to the bar is a review reject.
- The rendered-review loop (viewport matrix + pixel diff +
  vision_analyze) is the acceptance gate for UX changes; the reviewer
  model under-reads dark-on-dark effects, so pixel math arbitrates.
- The browser daemon chokes on repeated screenshots over blurred
  surfaces; split geometry checks from captures when a session wedges.
