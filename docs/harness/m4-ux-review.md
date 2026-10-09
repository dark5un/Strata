# M4 UX design review — findings and resolutions

Date: 2026-10-09. Reviewer: Hermes (against docs/harness/m4-ux-design.md).
Method: live mock-server browser pass (delegate flow end-to-end), computed-
style dark-theme audit (no vision encoder on this runtime — token resolution
+ WCAG-safe pairs checked programmatically instead), a11y attribute audit.

## BLOCKER (both applied)

1. **Reconnect loses a child's pending ask.** The relay only pushed into the
   parent's live approvals queue; a parent client reconnecting while a child
   ask was pending never saw it (the durable asked/decided rows only land at
   decision time). Fixed: `manager.pending_child_asks()` feeds both
   `/harness/events`' pending list and the SSE stream's reconnect replay,
   tagged with the child session id. Test:
   test_child_approval_reconnect_replays_relayed_ask.
2. **Decided-ask redraw race.** `deliver()` resolved the decision queue but
   `_asks` was only cleaned by the answerer thread's finally — a reconnect
   between the two redrew an already-answered card. Fixed: `deliver()` pops
   `_asks` under the lock (a decided ask stops being pending immediately).
   Caught by the new test on its first run.

## SHOULD (applied)

3. **Subagent card heading semantics.** The design doc says the card names
   the child session with a heading; the implementation used a bare span.
   Fixed: `h3.card-title` (styled to match, margin 0).
4. **Stop button honesty.** The design doc promises the Stop tooltip says
   when children are live (cancel cascades). Fixed: setBusy() counts running
   children from the session list and extends the tooltip.

## NICE (applied)

5. Export popover focuses its first button and Esc returns focus to the
   opener (goal popover got the same focus-return in the same commit).
6. Session rows carry `data-sid` (keyboard nav + child links both use it).

## NICE (deferred, recorded)

7. Subagent card could show the child's context gauge inline; the child
   session view already shows it on open. Not worth a second gauge.
8. The preset select could preview the persona in a tooltip per option —
   done (title attr carries the persona).

## Dark-theme audit (M4 acceptance item)

All M4 elements resolve through --st-* tokens in dark: subagent card
(accent-tint bg, ink text), badges (reading/generating tints), preset
select, Export button, notes, child link (accent). No light-theme
artifacts, no hardcoded colors in harness.css additions. WCAG: ink
#eef1f3 on tinted surfaces and accent #34d399 on #161a1d both pass.

## Verified live (mock server, browser)

delegate -> researcher child (shell/write/edit/delegate absent, persona
system section present) -> settlement wakes parent -> subagent card with
working child link -> export popover downloads markdown -> session list
shows sub badge + preset + indent -> arrow-key nav and "/" focus work.
