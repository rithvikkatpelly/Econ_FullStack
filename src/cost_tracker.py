"""
Lightweight token/cost estimation and a per-session budget guardrail.

Not a precise tokenizer match — the goal is a consistent, logged estimate
so tool results can be shaped *before* they're returned, and so the
caching/truncation choices in server.py can be justified with real numbers
instead of a hand-wavy claim.
"""

import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

# Rough heuristic: ~4 characters per token for English/JSON-ish text.
# Good enough for a budget guardrail; not a substitute for the real
# tokenizer if this were going to production.
_CHARS_PER_TOKEN = 4

LOG_PATH = Path(__file__).parent.parent / "usage.log"


def estimate_tokens(payload: str) -> int:
    return max(1, len(payload) // _CHARS_PER_TOKEN)


@dataclass
class SessionBudget:
    limit_tokens: int = int(os.environ.get("SESSION_TOKEN_BUDGET", "50000"))
    used_tokens: int = field(default=0)
    # The process-wide budget is hit by concurrent HTTP requests; check and
    # record must happen as one step or two callers can both "fit".
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def would_exceed(self, additional_tokens: int) -> bool:
        return (self.used_tokens + additional_tokens) > self.limit_tokens

    def try_record(self, tool_name: str, tokens: int, note: str = "") -> bool:
        """Record `tokens` if they fit; return whether they did. Atomic."""
        with self._lock:
            if self.would_exceed(tokens):
                return False
            self.used_tokens += tokens
        self._log(tool_name, tokens, note)
        return True

    def record(self, tool_name: str, tokens: int, note: str = "") -> None:
        with self._lock:
            self.used_tokens += tokens
        self._log(tool_name, tokens, note)

    def remaining(self) -> int:
        return max(0, self.limit_tokens - self.used_tokens)

    def _log(self, tool_name: str, tokens: int, note: str) -> None:
        line = (
            f"{time.strftime('%Y-%m-%dT%H:%M:%S')}\t{tool_name}\t"
            f"tokens={tokens}\trunning_total={self.used_tokens}\t{note}\n"
        )
        try:
            with open(LOG_PATH, "a") as f:
                f.write(line)
        except OSError:
            pass  # logging is best-effort, never block a tool call on it


# One shared budget per server process — what the MCP server and the HTTP
# tool endpoints spend from. An agent run gets its own instead (run_budget
# below), so concurrent questions can't drain each other or this one.
budget = SessionBudget()

# The budget of the run executing in this context, if any. A ContextVar so it
# follows the run into worker threads (asyncio.to_thread copies the context)
# without leaking into unrelated requests on other threads.
_run_budget: ContextVar[SessionBudget | None] = ContextVar("run_budget", default=None)


def current_budget() -> SessionBudget:
    """The budget a tool call made right here should spend from."""
    return _run_budget.get() or budget


@contextmanager
def run_budget(limit_tokens: int | None = None) -> Iterator[SessionBudget]:
    """Give everything inside the block its own fresh budget.

    The HTTP agent endpoints wrap each question in this, so one user's large
    query can't push another's over the limit, and neither touches the
    process-wide budget the tool endpoints use.
    """
    scoped = SessionBudget(limit_tokens=limit_tokens or SessionBudget().limit_tokens)
    token = _run_budget.set(scoped)
    try:
        yield scoped
    finally:
        _run_budget.reset(token)


def reset_budget() -> None:
    """Start a fresh session budget. The evaluation harness calls this between
    cases so one case's spend can't push the next over the limit; tests use it
    for isolation."""
    global budget
    budget = SessionBudget()


def guard_or_shrink(tool_name: str, payload: str, shrink_fn=None) -> tuple[str, dict]:
    """
    Check a would-be tool result against the session budget before
    returning it. If it fits, record the cost and return it as-is.
    If it doesn't fit and a shrink_fn was provided, try shrinking once
    (e.g. drop to monthly granularity, or truncate a series) and re-check.
    If it still doesn't fit, return a structured warning instead of the
    raw payload so the model can narrow the request.
    """
    active = current_budget()
    tokens = estimate_tokens(payload)

    if active.try_record(tool_name, tokens):
        return payload, {"estimated_tokens": tokens, "budget_remaining": active.remaining()}

    if shrink_fn is not None:
        shrunk = shrink_fn(payload)
        shrunk_tokens = estimate_tokens(shrunk)
        if active.try_record(tool_name, shrunk_tokens, note="shrunk"):
            return shrunk, {
                "estimated_tokens": shrunk_tokens,
                "budget_remaining": active.remaining(),
                "note": "Result was shrunk to fit the session token budget.",
            }

    return "", {
        "error": "session_budget_exceeded",
        "estimated_tokens": tokens,
        "budget_remaining": active.remaining(),
        "suggestion": "Narrow the date range, reduce the number of series, "
                       "or start a new session.",
    }


# --- Per-stage cost accounting (multi-agent pipeline) --------------------
#
# The orchestration layer logs an estimated cost for every agent hand-off.
# These are modelled numbers (the ~4-chars/token heuristic above, priced at
# claude-opus-5 list rates) so the trace has a cost column even when the
# pipeline runs fully offline.

INPUT_USD_PER_MTOK = 5.0
OUTPUT_USD_PER_MTOK = 25.0


@dataclass
class StageCost:
    stage: str
    input_tokens: int
    output_tokens: int

    @property
    def usd(self) -> float:
        return round(
            self.input_tokens / 1_000_000 * INPUT_USD_PER_MTOK
            + self.output_tokens / 1_000_000 * OUTPUT_USD_PER_MTOK,
            6,
        )

    def as_dict(self) -> dict:
        return {
            "stage": self.stage,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_usd": self.usd,
        }


def stage_cost(stage: str, sent: str, produced: str) -> StageCost:
    """Estimate the token cost of one agent turn from what it was sent and
    what it produced (both already serialized to strings)."""
    return StageCost(stage, estimate_tokens(sent), estimate_tokens(produced))


@dataclass
class RunCost:
    """Per-run cost accumulator for the multi-agent pipeline.

    `orchestration.run_query()` creates one of these per run and records every
    agent hand-off into it. `per_agent()` is the per-agent breakdown; `total_*`
    is the running total for the whole run. In phase 2 the parallel Data Agent
    calls all record into this same instance, so the total stays correct.
    """

    stages: list[StageCost] = field(default_factory=list)

    def record(self, cost: StageCost) -> StageCost:
        self.stages.append(cost)
        return cost

    def add(self, stage: str, sent: str, produced: str) -> StageCost:
        return self.record(stage_cost(stage, sent, produced))

    @property
    def total_input_tokens(self) -> int:
        return sum(s.input_tokens for s in self.stages)

    @property
    def total_output_tokens(self) -> int:
        return sum(s.output_tokens for s in self.stages)

    @property
    def total_usd(self) -> float:
        return round(sum(s.usd for s in self.stages), 6)

    def per_agent(self) -> list[dict]:
        """One row per agent name, collapsing repeated calls (phase 2's
        parallel Data Agents) while keeping a call count."""
        rows: dict[str, dict] = {}
        for s in self.stages:
            row = rows.setdefault(
                s.stage,
                {"stage": s.stage, "calls": 0, "input_tokens": 0,
                 "output_tokens": 0, "estimated_usd": 0.0},
            )
            row["calls"] += 1
            row["input_tokens"] += s.input_tokens
            row["output_tokens"] += s.output_tokens
            row["estimated_usd"] = round(row["estimated_usd"] + s.usd, 6)
        return list(rows.values())

    def as_dict(self) -> dict:
        return {
            "per_agent": self.per_agent(),
            "total": {
                "input_tokens": self.total_input_tokens,
                "output_tokens": self.total_output_tokens,
                "estimated_usd": self.total_usd,
            },
        }
