"""
Follow-up questions: how earlier turns of a conversation reach the supervisor.

The supervisor is stateless — it sees one user message. For a follow-up
("what about since 2015?") that message carries the earlier turns as a
labelled data block, then the current question:

    Earlier turns in this conversation, for context only:
    {"untrusted_source": "conversation_history", "untrusted_source_text": "Q: …\\nA: …", …}

    Current question: what about since 2015?

The history comes from the browser, so it is user-controlled: it is wrapped
with `security.wrap_untrusted_text` (the same treatment as FRED notes),
limited to the last few turns, and each earlier answer is clipped. It can
tell the model what a follow-up refers to; it is not a channel for
instructions. The deterministic stub planner (agents/stub.py) uses `split`
to recover the two parts.
"""

from __future__ import annotations

import json

import security

MAX_TURNS = 3
MAX_ANSWER_CHARS = 1500

_INTRO = "Earlier turns in this conversation, for context only:"
_MARKER = "\n\nCurrent question: "
_SOURCE = "conversation_history"


def compose(query: str, history: list[dict] | None) -> str:
    """The supervisor's opening message: `query` alone, or the bounded,
    wrapped history followed by `query`."""
    turns = [t for t in (history or []) if t.get("query")][-MAX_TURNS:]
    if not turns:
        return query
    rendered = "\n\n".join(
        f"Q: {t['query']}\nA: {_clip(t.get('answer', ''))}" for t in turns
    )
    block = json.dumps(security.wrap_untrusted_text(_SOURCE, rendered), ensure_ascii=False)
    return f"{_INTRO}\n{block}{_MARKER}{query}"


def split(message: str) -> tuple[list[tuple[str, str]], str]:
    """Inverse of `compose`: ([(question, answer), …], current question).
    A message without history comes back as ([], message)."""
    if not message.startswith(_INTRO) or _MARKER not in message:
        return [], message
    head, question = message.split(_MARKER, 1)
    try:
        wrapped = json.loads(head.removeprefix(_INTRO).strip())
        rendered = wrapped["untrusted_source_text"]
    except (ValueError, KeyError, TypeError):
        return [], question
    turns = []
    for chunk in rendered.split("\n\nQ: "):
        q, _, a = chunk.removeprefix("Q: ").partition("\nA: ")
        turns.append((q, a))
    return turns, question


def _clip(text: str) -> str:
    text = text.strip()
    if len(text) <= MAX_ANSWER_CHARS:
        return text
    return text[:MAX_ANSWER_CHARS] + "…"
