"""serve/harness/tools/ask.py — ask_user: the model asks, the human answers.

The tool blocks the turn on a threading.Event the routes resolve when the
browser posts an answer (dsh's user-questions seam, simplified). A timeout
releases the model while the question stays answerable later; a timeout is
never an approval.
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field

from ..loop import ToolDefinition


@dataclass
class Question:
    qid: str
    question: str
    options: list[dict] = field(default_factory=list)
    multi: bool = False
    answered: threading.Event = field(default_factory=threading.Event)
    answer: dict | None = None


class QuestionBoard:
    """Pending questions for one session; the SSE stream and the answer route meet here."""

    def __init__(self) -> None:
        self._pending: dict[str, Question] = {}
        self._lock = threading.Lock()

    def ask(self, question: str, options: list[dict], multi: bool) -> Question:
        q = Question(qid=uuid.uuid4().hex[:10], question=question, options=options, multi=multi)
        with self._lock:
            self._pending[q.qid] = q
        return q

    def answer(self, qid: str, answer: dict) -> bool:
        with self._lock:
            q = self._pending.pop(qid, None)
        if q is None:
            return False
        q.answer = answer
        q.answered.set()
        return True

    def pending(self) -> list[Question]:
        with self._lock:
            return list(self._pending.values())

    def cancel_all(self) -> None:
        with self._lock:
            qs = list(self._pending.values())
            self._pending.clear()
        for q in qs:
            q.answer = {"status": "cancelled"}
            q.answered.set()


def ask_tool(board: QuestionBoard) -> ToolDefinition:
    def execute(args: dict, ctx) -> dict:
        questions = args.get("questions") or [{"question": args.get("question", "")}]
        if len(questions) != 1:
            # v1: one question per call keeps the UI simple; the model can call it again.
            questions = questions[:1]
        spec = questions[0]
        q = board.ask(str(spec.get("question", "")), list(spec.get("options") or []),
                      bool(spec.get("multiSelect")))
        timeout = float(args.get("timeout_s") or 0)
        if q.answered.wait(timeout=timeout if timeout > 0 else None):
            ans = q.answer or {}
            return {"text": f"answer: {ans.get('selected') or ans.get('text') or ans.get('status')}",
                    "is_error": ans.get("status") == "cancelled"}
        return {"text": f"question {q.qid} is still waiting for the user; continue and it will be answered later",
                "is_error": False, "meta": {"qid": q.qid, "timed_out": True}}

    return ToolDefinition(
        name="ask_user",
        description="Ask the user one question with optional choices. Blocks until answered or timed out.",
        parameters={"type": "object", "properties": {
            "questions": {"type": "array", "items": {"type": "object", "properties": {
                "question": {"type": "string"},
                "options": {"type": "array", "items": {"type": "object",
                            "properties": {"label": {"type": "string"}, "description": {"type": "string"}}}},
                "multiSelect": {"type": "boolean"}}}},
            "timeout_s": {"type": "number"}},
            "required": ["questions"]},
        execute=execute, concurrency_safe=False, timeout_s=3600)
