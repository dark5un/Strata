# M6 UX review record (2026-10-09)

Rendered pass per docs/harness/viewport-matrix.md (mock/live-5090 tab
server, geometry + vision at 1920/1001/640).

## BLOCKERs found -> applied

1. **Result label clipped by the composer** — a tool result landing while
   the reader sits at the bottom left the new "Result · …" subhead
   half-hidden. Applied: fillToolCard() now sticks the scroll when the
   reader was at the bottom (same rule as el()).
2. **Sidebar rows unreadable** — the running pill + event count crowded
   the title to "L…". Applied: the event count and id move to the row
   tooltip; the row shows icon + title + state badges only. Unreadable
   sessions get an "unreadable" badge instead of the count text.

## SHOULD -> applied

3. Hex-id rows still visible for sessions with no user message yet —
   correct fallback (a brand-new session has no title); kept, with the
   id in the tooltip.
4. Preset badge: was plain muted text, now st-badge like sub/running
   (uniform row language).

## NICE (deferred, not applied)

5. Tool-card header repeats the command in name+preview at short widths;
   acceptable — the name is the tool, the preview is the payload.
6. Approval card could show the result kind too; the preview is already
   the command itself.

## Verdict

M6 items D1-D7 all rendered and verified: markdown bubbles (p/li/strong/
pre + language chip + copy button), titles, per-tool previews,
"Arguments · shell command" / "Result · tabular output (325 chars,
12 lines)" labels, badge tooltip carrying the size, caret while working,
gauge tooltip. Gates green (129 tests).
