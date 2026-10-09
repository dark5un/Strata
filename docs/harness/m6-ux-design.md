# M6 UX design pass — content polish (2026-10-09)

Driver: the user's live 5090 session — "the UI still is a bit unpolished",
plus "provide the kind of content the response is showing next to
arguments, result etc". Bar to match: the Chat tab (the user's own
screenshots: flat prose, markdown rendered, code cards with language chip
+ copy button, one accent colour, quiet affordances).

## Design principles for this milestone

1. The transcript is a *document*, not a debug dump. Every element tells
   you what it is before you expand it.
2. Information belongs where the decision is made: the collapsed summary
   answers "is this worth opening?", the section labels answer "what am I
   looking at inside?".
3. Reuse Chat-tab primitives exactly (markdown(), st-code, tool-call
   pattern). No new visual vocabulary — the fork rule.

## Decisions

D1. Assistant bubbles render markdown via the Chat tab's markdown()
    (safe: escapes first, http(s)-only links). Code fences get the
    language chip + copy button for free. User bubbles stay plain text
    (what the user typed is not markdown).
    Placement: inside .st-bubble, same as Chat.

D2. Session list rows show the first real user message (60 chars, one
    line) as the title; the hex id moves to the row's tooltip. Forks
    inherit the title (the prefix carries the message). Empty title
    (no message yet) falls back to the id.

D3. Tool card, collapsed summary: name + one-line preview of what the
    tool touches (shell -> the command; fs -> the path; search -> the
    pattern), state badge. NOT raw JSON.
    Rationale: the summary is the scan layer; JSON is noise there.

D4. Tool card, expanded sections: the *content kind* rides the section
    label, muted and small, after a middot:
      "Arguments · shell command"
      "Result · 10 lines, 486 chars"
    The kind answers "what format is this block"; the size answers
    "how much am I about to read". Both belong at the label because
    that is where the reader decides to skim or read.
    The summary line does NOT repeat the size (D3 keeps it scannable);
    the state badge tooltip carries it instead.

D5. Approval card: "Run `shell`?" — the tool name in code style, the
    command preview in the st-code card. The decision the user makes is
    about the command, so the command is the hero of the card.

D6. Streaming: a blinking caret line ("working") at the end of the
    transcript while busy; removed on turn end. Honours
    prefers-reduced-motion.

D7. Context gauge: tooltip names the denominator ("of this server's
    engine context window") — honest about the HTTP-attached trial shape.

## Result-kind vocabulary (D4)

JSON · directory listing · tabular output · diff · plain text, always
with "N lines, M chars". Unknown/short text -> plain text.

## A11y

Titles are text nodes (screen-reader reachable); tooltips are
supplementary, never the only carrier; caret is aria-hidden decoration —
the state pill already announces busy via aria-live.
