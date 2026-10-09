# Harness tab — visual design review (2026-10-09)

First review done on the *rendered* tab (browser at 1280x577 + vision analysis),
not code-level like the M2-M4 reviews. The user's complaint: "the prompt input
field is small, buttons are not uniform, not beautiful to the sight." The
rendered pass proved them right and found the root causes.

## BLOCKERs (all fixed)

1. **Composer collapsed to 233px** (chat's is 860px). Root cause: app.css's
   `.st-composer { margin: 0 auto }` makes the flex child size to content
   inside `.harness-main`. Fix: `.harness-main .st-composer` pins
   `width: 100%; max-width: 860px; margin-inline: auto`.
2. **No page padding**: the layout touched the header and window edges
   (chat has `--st-s-6` gutters). Fix: `.harness-layout` padding +
   `gap: var(--st-s-4)`, sidebar 240px.
3. **Toolbar wider than the column beneath it** — bar spanned the full main
   area while transcript/composer sat centered: three different horizontal
   centers. Fix: `.harness-bar` shares the 860px measure; transcript rows
   capped to it too (`.harness-transcript > *`).
4. **Control sizes not uniform in the bar**: selects 44px vs buttons 42px.
   Fix: bar selects/inputs `height: 42px`.
5. **Send button wrong variant**: ghost 36px icon button vs chat's 42px
   primary (`st-btn--primary st-btn--send`), and it glowed green while the
   composer was disabled. Fix: markup parity + `disabled` rides
   composerEnabled(); the session state pill hides when no session is open
   (it duplicated the header pill).
6. **No empty state**: blank void where chat shows "Ask anything". Fix:
   `.harness-empty` hero (reuses chat-empty classes) shown via
   `.harness-transcript:empty`.
7. **Textarea never grows** (chat autosizes to 40vh). Fix: autosizeHarness()
   on input + after send, same formula as app.js.

## Accepted as upstream-primitive (NOT forked)

Violet focus ring, tool-card internals, scrollbar arrows, sidebar icon
tinting: all come from components.css/app.css shared with the Chat tab. The
fork rule is "design-system reuse, no new primitives" — changing these means
changing the Chat tab too; out of scope for the harness fork.

## Method note (for future reviews)

Geometry-first: measure `getBoundingClientRect()` of every control in one js()
call, compare against the Chat tab's equivalents — catches collapse/misalign
bugs without needing to see pixels. Then vision_analyze on the screenshot for
what geometry can't judge (balance, affordance, color noise). The running
Strata instance needs the vision encoder for this: VISION=cpu in
ai-lab-quadlets service.env (2026-10-09) makes it free on any variant.
