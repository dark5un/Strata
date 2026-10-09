# ADR-0004: Event-sourced sessions — append-only JSONL, derived history

Date: 2026-10-09
Status: accepted

## Context
dsh's strongest architectural idea (research/dsh-agent-core.md §2, §7): the
session is an append-only log of typed events; the LLM message history is
*derived* from a surface projection of that log, never stored twice. Resume,
fork, compaction, audit and crash repair all fall out of the log.

## Decision
Adopt the pattern (independently implemented, Python-shaped):
- `serve/harness/session.py`: one JSONL file per session under
  `~/.local/share/strata/harness/sessions/<id>.jsonl` (in-container:
  $DSH-style volume path). Envelope: {type, seq, time, data}. seq is
  monotonic and contiguous; readers ignore unknown types (forward-compat).
- Event set (v0, trimmed from dsh's 13+): turn/start,end; step/start,end;
  user/message; assistant/message (with usage + timed stream records);
  tool/call; tool/result; request/header (full GenerateOptions snapshot —
  every request reconstructable); approval/asked,decided; compaction/start,
  summary,end; session/end-seed (fork marker).
- Surface projection with `surfaceOp: {op:'replace', startSeq, endSeq}`:
  compaction appends a summary node that shadows a span; the raw log is never
  rewritten. deriveMessages() folds the surface.
- Crash repair: on resume, append synthetic closers (turn/end {interrupted},
  TOOL_NOT_STARTED results) — storage never truncates. Tool-call/result
  pairing stays balanced at every prefix, so derived history is always
  provider-valid.
- Fork: copy an exact event prefix (must end outside an open turn), append
  session/end-seed; header carries parentSession + depth.
- Format version field in the header; unknown major version = refuse to open
  (no silent partial replay).

## Consequences
- Simplest durable thing that works: plain JSONL, fsync per batch, no zstd in
  v0 (add later if logs get big — it is an internal format detail).
- Single-writer per session (the owning worker thread); the SSE reader takes
  a tail-follow position, never writes.
- Tests can assert on event sequences directly — the log is the API.
