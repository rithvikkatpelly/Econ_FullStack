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
            b = self._buckets.get(key)
            if b is None:
                b = _Bucket(tokens=self.capacity, updated=now)
                self._buckets[key] = b

            elapsed = now - b.updated
            b.tokens = min(self.capacity, b.tokens + elapsed * self.refill_per_sec)
            b.updated = now

            if b.tokens >= 1.0:
                b.tokens -= 1.0
                return True, 0.0

            deficit = 1.0 - b.tokens
            if not self.refill_per_sec:
                return False, float("inf")
            return False, round(deficit / self.refill_per_sec, 3)

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
