# M7 UX review — rendered pass (2026-10-09)

Method: viewport matrix (docs/harness/viewport-matrix.md) on the live 8196
tab server (harness factory -> 5090 over HTTP), cache-ignoring reloads,
getBoundingClientRect() measurements vs the Chat tab, vision_analyze on the
shots, plus DOM assertions for small text the vision model garbles (its
"DevOps"/"Early knowledge" readings were misreads of "Duplicate"/"editing
copies them" — DOM checks are the record here).

## Geometry (measured)

| width | bar | composer | sidebar | resizer | yolo banner | pre overflow-x |
|---|---|---|---|---|---|---|
| 1920 | 860 | 860 | 240 | 4 | 860 (after fix) | none |
| 1440 | 860 | 860 | 240 | 4 | 860 | none |
| 1001 | 677 | 677 | 240 | 4 | 677 | none |
| 999  | 675 | 675 | 240 | 4 | 675 | none |
| 640  | 592 | 592 | 592 (stacked) | hidden | 592 | none |

Type hierarchy vs the Chat tab at 1920: bubble 16px/24.8, tool name 13px,
pre 12.5px — identical in both tabs (D5 satisfied: the harness reuses the
Chat tab's classes, no invented sizes).

## Live behaviour verified

- Streaming: 26 `stream/delta` rows over SSE during one turn; provisional
  bubble + Thinking block paint and finalize (DOM + probe).
- Reasoning: durable on assistant/message, collapsed "Thinking" fold in the
  transcript, never replayed into derived history (test + live log).
- Approval at decision time: ask card showed the FULL command
  (`find serve/harness -type f | wc -l`), scrollable pre; Allow once folded
  it with the full payload in the expand.
- Yolo: no ask card, quiet fold badged `yolo` with the full command, banner
  persistent, pill warn state, durable `approval/decided {yolo, asked:false}`.
- Session titles: model-generated ("hello there", "the sea is calm") in the
  sidebar; truncation fallback for pre-M7 sessions.
- Resizer: drag + arrow keys, 160-480 clamp, persisted; hidden at 640.
- Tab order: Chat -> Harness -> Monitor -> About (user request).

## Findings

BLOCKER-1 (applied): decided approvals vanished on replay — the fold only
existed when the ask card was painted live; after a reload the durable
asked/decided pair painted nothing, hiding what was approved. Fix: the
`tool/call` row seeds full args into H.askMeta, `approval/asked` merges its
preview, and a missing live card falls back to `approvalFold()` with the
quiet style (`data-decided` set on the replay path too). Verified: reload
shows the fold with the full command.

BLOCKER-2 (applied): `approval/decided` did not carry the tool name, so a
replayed yolo fold (no ask card, no preceding asked row) would have lost
its label. Fix: loop appends `tool` on the decided row (forward-compat).

SHOULD-1 (applied): the yolo banner ignored the 860px reading measure
(1116px at 1920 while bar/composer sit at 860). Fix: max-width 860 +
margin-inline auto; measured 860 at 1920/1440, shrinks with the column.

SHOULD-2 (applied): the state pill never showed the yolo warn colour while
working (setBusy overwrote it). Fix: setBusy reads the banner state and
picks `yolo` as the pill state.

NOT A BUG (vision misreads): preset popover copy, row actions and containment
verified in the DOM (420px popover, description inside, Duplicate/Edit/Delete
buttons render; the earlier shot's garbled text was the vision model, not
the page).

## Verdict

All BLOCKER/SHOULD findings applied and re-verified live. M7 closes with
gate green (149 tests + ruff + pyright + eslint) and slice branch pr/7-m7.
