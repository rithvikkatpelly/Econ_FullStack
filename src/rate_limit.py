"""
Token-bucket rate limiting.

Purpose: the MCP server is the one place an *untrusted* caller (a model, or
whatever is driving it) reaches our code. A misbehaving or adversarial client
can call a tool in a tight loop — running up the FRED quota, the token
budget, and the bill. This caps the call rate per (client, tool) and returns
a structured `rate_limited` error the model can back off on, rather than
letting the loop run.

Not wired into the in-process agent orchestrator: that's trusted code with
its own per-agent iteration cap (agents/base.py). This guards the boundary.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass


def take(
    tokens: float, updated: float, now: float, capacity: float, refill_per_sec: float
) -> tuple[float, bool, float]:
    """One token-bucket step: refill for the time since `updated`, then try to
    spend one token. Returns (tokens left, allowed, retry_after_seconds). Pure,
    so the in-memory and the Firestore limiter share it."""
    tokens = min(capacity, tokens + max(0.0, now - updated) * refill_per_sec)
    if tokens >= 1.0:
        return tokens - 1.0, True, 0.0
    if not refill_per_sec:
        return tokens, False, float("inf")
    return tokens, False, round((1.0 - tokens) / refill_per_sec, 3)


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    """`capacity` tokens, refilled at `refill_per_sec`. Each call costs 1."""

    def __init__(self, capacity: float, refill_per_sec: float, *, clock=time.monotonic):
        self.capacity = float(capacity)
        self.refill_per_sec = float(refill_per_sec)
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, float]:
        """Returns (allowed, retry_after_seconds). retry_after is 0 when allowed."""
        now = self._clock()
        with self._lock:
            b = self._buckets.get(key) or _Bucket(tokens=self.capacity, updated=now)
            b.tokens, allowed, retry_after = take(
                b.tokens, b.updated, now, self.capacity, self.refill_per_sec
            )
            b.updated = now
            self._buckets[key] = b
            return allowed, retry_after

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


def _default_limiter() -> RateLimiter:
    per_min = float(os.environ.get("TOOL_RATE_LIMIT_PER_MIN", "120"))
    burst = float(os.environ.get("TOOL_RATE_LIMIT_BURST", "30"))
    return RateLimiter(capacity=burst, refill_per_sec=per_min / 60.0)


limiter = _default_limiter()


def guard(client_id: str, tool_name: str, bucket: RateLimiter | None = None) -> dict | None:
    """Return a structured error dict if the call should be rejected, else None."""
    allowed, retry_after = (bucket or limiter).check(f"{client_id}:{tool_name}")
    if allowed:
        return None
    return {
        "error": "rate_limited",
        "detail": f"Rate limit for '{tool_name}' exceeded.",
        "retry_after_seconds": retry_after,
    }
