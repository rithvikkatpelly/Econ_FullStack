"""Agent endpoints — the multi-agent supervisor over HTTP.

The tool endpoints in ``app.main`` are one HTTP call per tool. These two run
the *whole* pipeline (``src/agents``: supervisor → Economic Data / Research /
Risk / Report agents) on a plain-language question:

- ``POST /agent/ask``    — run to completion, return the full trace as JSON.
- ``POST /agent/stream`` — same run, streamed as server-sent events so a UI
  can draw an activity timeline while the agents work.

The orchestrator is whatever ``AGENT_FRAMEWORK`` selects — ``adk`` (Google's
Agent Development Kit, ``src/econ_adk``; the default) or ``native``
(``src/agents``) — and the model whatever ``AGENT_BACKEND`` selects:
``gemini`` in the deployed app, ``stub`` (deterministic, keyless) by default
and in tests. The endpoints don't know or care which.

Stream format: one ``data: <json>\\n\\n`` frame per event, each with a
``type``:

    start         {query, backend, framework, follow_up}
    delegation    {agent, task}                       supervisor hands off
    tool_call     {agent, tool, arguments, ok, error, latency_ms}
    report_delta  {agent, text}                       answer text as it's written
    waiting       {agent, reason, seconds}            paused for a provider quota
    fallback      {agent, reason, from_model, to_model, restart?}
                                                      a model was overloaded or out of
                                                      quota; `restart` = the answer so
                                                      far is void, the run starts over
    agent_output  {agent, output}                     a specialist finished
    final         {final_report, series_used, risk_signal, tokens,
                   backend, degraded}                 `backend` is the one that answered
    error         {error, detail}                     the run failed

``final`` or ``error`` is always the last frame. Tool *results* are never
streamed (see ``agents/trace.py``) — only what was called and whether it
worked — so raw FRED/news text can't reach the browser this way.

Guardrails, because a live run costs real model calls: a per-client token
bucket (``AGENT_RATE_LIMIT_*``), a cap on concurrent runs, and a query length
limit. All three reject with the same structured error shape as the tool
endpoints. And a free-tier demo stays up when the day's Gemini quota is gone:
with ``AGENT_STUB_FALLBACK`` (the default) the question is answered on the
offline stub instead, labelled as such (``final.degraded``).

Each run also spends tool data from its own token budget
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
import time
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

import cost_tracker
from agents import conversation
from agents.model import QuotaExhausted, backend_override
from agents.supervisor import framework, run_with_framework
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


def _run_once(req: AskRequest, trace: Trace) -> dict:
    """Run one question under its own token budget; return its data usage."""
    history = [t.model_dump() for t in req.history]
    with cost_tracker.run_budget(settings.agent_run_token_budget) as budget:
        run_with_framework(req.query, trace, history)
    return {"data_tokens": budget.used_tokens, "data_token_budget": budget.limit_tokens}


def _run(
    req: AskRequest, listener: Callable[[dict], None] | None = None
) -> tuple[Trace, dict]:
    """Answer one question. Returns the trace and a small dict for the
    response: data usage, the backend that actually answered, and why it
    differs from the configured one (`degraded`), if it does.

    On Gemini with AGENT_STUB_FALLBACK, a used-up quota doesn't fail the
    question: it's answered again on the offline stub, and live calls pause
    until the quota resets (`live_paused`) so later questions go straight to
    the stub instead of each spending a minute finding out."""
    configured = backend_name()
    fallback = configured == "gemini" and settings.agent_stub_fallback
    if fallback and live_paused():
        trace = Trace(listener=listener)
        trace.emit(_stub_fallback_event(restart=False))
        with backend_override("stub"):
            usage = _run_once(req, trace)
        return trace, {**usage, "backend": "stub", "degraded": "model_quota_exhausted"}

    trace = Trace(listener=listener)
    try:
        usage = _run_once(req, trace)
    except QuotaExhausted as exc:
        if not fallback:
            raise
        _pause_live(exc.retry_after)
        logger.warning(
            "gemini quota exhausted; answering on the stub",
            extra={"fields": {"paused_until": _live_paused_until}},
        )
        # Whatever Gemini already streamed is abandoned: tell the UI to clear
        # it, then answer from scratch on a fresh trace (same listener, same
        # clock).
        trace.emit(_stub_fallback_event(restart=True))
        retry = Trace(listener=listener)
        retry.started_at = trace.started_at
        with backend_override("stub"):
            usage = _run_once(req, retry)
        return retry, {**usage, "backend": "stub", "degraded": "model_quota_exhausted"}
    return trace, {**usage, "backend": configured, "degraded": None}


def _stub_fallback_event(restart: bool) -> dict:
    return {
        "type": "fallback", "agent": "supervisor", "reason": "quota_exhausted",
        "from_model": "Gemini", "to_model": "offline stub", "restart": restart,
    }


# --- pausing live runs once the quota is gone -------------------------------
# Gemini API free-tier quotas are per day and reset at midnight Pacific time.
_RESET_TZ = ZoneInfo("America/Los_Angeles")
_live_paused_until = 0.0  # epoch seconds
_pause_lock = threading.Lock()


def _next_quota_reset(now: float) -> float:
    local = datetime.fromtimestamp(now, _RESET_TZ)
    midnight = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()


def _pause_live(retry_after: float | None, now: float | None = None) -> None:
    """Stop calling Gemini for Google's `retry_after` estimate when it gave
    one, else until the daily reset."""
    global _live_paused_until
    now = time.time() if now is None else now
    until = now + retry_after if retry_after else _next_quota_reset(now)
    with _pause_lock:
        _live_paused_until = max(_live_paused_until, until)


def live_paused() -> bool:
    with _pause_lock:
        return _live_paused_until > time.time()


def reset_live_pause() -> None:
    global _live_paused_until
    with _pause_lock:
        _live_paused_until = 0.0


def effective_backend() -> str:
    """The backend the next question will run on."""
    configured = backend_name()
    if configured == "gemini" and settings.agent_stub_fallback and live_paused():
        return "stub"
    return configured


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
    return {"framework": framework(), **trace.to_dict(), **usage}


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
    backend = effective_backend()
    orchestrator = framework()

    def push(event: dict | None) -> None:
        # Called from the worker thread. If the loop is gone (server
        # shutting down) there's no one left to tell.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(queue.put_nowait, event)

    def work() -> None:
        try:
            trace, usage = _run(req, listener=push)
            push({"type": "final", "framework": orchestrator, **trace.summary(), **usage})
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
            "framework": orchestrator,
            "follow_up": bool(req.history),
        })
        while (event := await queue.get()) is not None:
            yield _frame(event)

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
