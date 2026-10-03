"""Agent endpoints — the multi-agent supervisor over HTTP.

The tool endpoints in ``app.main`` are one HTTP call per tool. These two run
the *whole* pipeline (``src/agents``: supervisor → Economic Data / Research /
Risk / Report agents) on a plain-language question:

- ``POST /agent/ask``    — run to completion, return the full trace as JSON.
- ``POST /agent/stream`` — same run, streamed as server-sent events so a UI
  can draw an activity timeline while the agents work.

The model behind the agents is whatever ``AGENT_BACKEND`` selects — ``gemini``
in the deployed app, ``stub`` (deterministic, keyless) by default and in
tests. The endpoints don't know or care which.

Stream format: one ``data: <json>\\n\\n`` frame per event, each with a
``type``:

    start         {query, backend, follow_up}
    delegation    {agent, task}                       supervisor hands off
    tool_call     {agent, tool, arguments, ok, error, latency_ms}
    report_delta  {agent, text}                       answer text as it's written
    waiting       {agent, reason, seconds}            paused for a provider quota
    fallback      {agent, from_model, to_model}       primary model overloaded
    agent_output  {agent, output}                     a specialist finished
    final         {final_report, series_used, risk_signal, tokens, ...}
    error         {error, detail}                     the run failed

``final`` or ``error`` is always the last frame. Tool *results* are never
streamed (see ``agents/trace.py``) — only what was called and whether it
worked — so raw FRED/news text can't reach the browser this way.

Guardrails, because a live run costs real model calls: a per-client token
bucket (``AGENT_RATE_LIMIT_*``), a cap on concurrent runs, and a query length
limit. All three reject with the same structured error shape as the tool
endpoints. Each run also spends tool data from its own token budget
(``AGENT_RUN_TOKEN_BUDGET``), never the process-wide one the tool endpoints
share, so concurrent questions can't starve each other.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import threading
from collections.abc import AsyncIterator, Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

import cost_tracker
from agents import conversation
from agents.model import QuotaExhausted
from agents.supervisor import Supervisor
from agents.trace import Trace
from core.config import get_settings
from rate_limit import RateLimiter

settings = get_settings()
logger = logging.getLogger("econ_data_api")

router = APIRouter(prefix="/agent", tags=["agent"])

_limiter = RateLimiter(
    capacity=settings.agent_rate_limit_burst,
    refill_per_sec=settings.agent_rate_limit_per_min / 60.0,
)
_slots = threading.BoundedSemaphore(settings.agent_max_concurrent_runs)


class PriorTurn(BaseModel):
    """One earlier question/answer pair, sent back by the client for a
    follow-up. User-controlled: bounded here, wrapped as data downstream
    (agents/conversation.py)."""

    query: str = Field(..., max_length=2000)
    answer: str = Field("", max_length=8000)


class AskRequest(BaseModel):
    history: list[PriorTurn] = Field(
        default_factory=list,
        max_length=conversation.MAX_TURNS,
        description="Earlier turns, oldest first, so a follow-up can refer to them.",
    )
    query: str = Field(
        ...,
        description="Plain-language question, e.g. 'Compare CPI and unemployment since 2019.'",
        examples=["Compare CPI and unemployment over the last 5 years."],
    )

    @field_validator("query")
    @classmethod
    def _bounded(cls, v: str) -> str:
        v = " ".join(v.split())
        if len(v) < 3:
            raise ValueError("query is too short")
        if len(v) > settings.agent_max_query_chars:
            raise ValueError(f"query is longer than {settings.agent_max_query_chars} characters")
        return v


def backend_name() -> str:
    return os.environ.get("AGENT_BACKEND", "stub").strip().lower() or "stub"


def _client_id(request: Request) -> str:
    # Cloud Run's front end appends the caller's address to X-Forwarded-For;
    # the first hop is the original client.
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _admit(request: Request) -> None:
    """Rate-limit then reserve a run slot, or raise. The caller must release
    the slot (``_slots.release()``) once the run is finished."""
    allowed, retry_after = _limiter.check(_client_id(request))
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "rate_limited",
                "detail": "Too many agent questions from this client.",
                "suggestion": f"Try again in {max(1, round(retry_after))} seconds.",
            },
            headers={"Retry-After": str(max(1, round(retry_after)))},
        )
    if not _slots.acquire(blocking=False):
        raise HTTPException(
            status_code=503,
            detail={
                "error": "agent_busy",
                "detail": "The agent is already answering the maximum number of questions.",
                "suggestion": "Try again in a few seconds.",
            },
        )


def _run(
    req: AskRequest, listener: Callable[[dict], None] | None = None
) -> tuple[Trace, dict]:
    """Run one question under its own token budget. Returns the trace and a
    small usage dict for the response."""
    trace = Trace(listener=listener)
    history = [t.model_dump() for t in req.history]
    with cost_tracker.run_budget(settings.agent_run_token_budget) as budget:
        Supervisor(trace).run(req.query, history)
    return trace, {
        "data_tokens": budget.used_tokens,
        "data_token_budget": budget.limit_tokens,
    }


def _failure(exc: Exception) -> dict:
    logger.exception(
        "agent run failed",
        extra={"fields": {"backend": backend_name(), "exc_type": type(exc).__name__}},
    )
    if isinstance(exc, QuotaExhausted):
        # Not a bug and not the user's fault: say so, so they wait instead of
        # rephrasing. Still no provider text.
        return {
            "error": "model_quota_exhausted",
            "detail": "The AI model's usage quota is used up for the moment.",
            "suggestion": "Try again in a minute or two.",
        }
    # Never echo the exception text — provider errors can include request
    # details. The log line above has what an operator needs.
    return {
        "error": "agent_error",
        "detail": "The agent couldn't finish this question.",
        "suggestion": "Try rephrasing, or ask about a specific indicator and date range.",
    }


@router.post("/ask")
def ask(req: AskRequest, request: Request) -> dict:
    """Run the supervisor to completion. Returns the full trace — every
    delegation and tool call, token usage, timing, and the final report."""
    _admit(request)
    try:
        trace, usage = _run(req)
    except Exception as exc:  # noqa: BLE001 - mapped to a structured 502
        status = 429 if isinstance(exc, QuotaExhausted) else 502
        raise HTTPException(status_code=status, detail=_failure(exc)) from None
    finally:
        _slots.release()
    return {"backend": backend_name(), **trace.to_dict(), **usage}


def _frame(event: dict) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


@router.post("/stream")
async def stream(req: AskRequest, request: Request) -> StreamingResponse:
    """Run the supervisor and stream its progress as server-sent events (see
    the module docstring for the event types). POST + fetch rather than
    EventSource, so the question travels in a JSON body, not a URL."""
    _admit(request)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict | None] = asyncio.Queue()
    backend = backend_name()

    def push(event: dict | None) -> None:
        # Called from the worker thread. If the loop is gone (server
        # shutting down) there's no one left to tell.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(queue.put_nowait, event)

    def work() -> None:
        try:
            trace, usage = _run(req, listener=push)
            push({"type": "final", "backend": backend, **trace.summary(), **usage})
        except Exception as exc:  # noqa: BLE001 - becomes an `error` frame
            push({"type": "error", **_failure(exc)})
        finally:
            _slots.release()
            push(None)

    # The run keeps going if the client disconnects — it's already paid for,
    # and the slot is released when it actually ends, not when the socket does.
    loop.run_in_executor(None, work)

    async def frames() -> AsyncIterator[str]:
        yield _frame({
            "type": "start",
            "query": req.query,
            "backend": backend,
            "follow_up": bool(req.history),
        })
        while (event := await queue.get()) is not None:
            yield _frame(event)

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
