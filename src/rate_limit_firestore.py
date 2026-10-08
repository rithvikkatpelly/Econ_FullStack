"""
A rate limiter shared by every instance of a service, in Firestore.

`rate_limit.RateLimiter` keeps its buckets in process memory, so a service
running N instances gives a client up to N times the intended rate. This one
keeps each bucket in a Firestore document and updates it in a transaction,
so every instance draws from the same bucket. Same token-bucket arithmetic
(`rate_limit.take`), same `check(key)` contract.

Firestore's free tier (50k reads and 20k writes a day) covers one check per
agent question many times over; Memorystore, the obvious alternative, has no
free tier. Opt in with RATE_LIMIT_BACKEND=firestore (backend/app/agent.py).

If Firestore can't be reached, a check falls back to the instance's
in-memory bucket rather than failing the request: a rate limiter must not
take the API down.
"""

from __future__ import annotations

import hashlib
import logging
import time

from rate_limit import RateLimiter, take

logger = logging.getLogger("econ_data_api")

COLLECTION = "rate_limits"
# Buckets of clients who stopped calling are deleted by a Firestore TTL policy
# on this field (DEPLOYMENT.md); a bucket idle this long is full again anyway.
_EXPIRE_AFTER_S = 24 * 3600


class FirestoreRateLimiter:
    def __init__(self, capacity: float, refill_per_sec: float, *, client=None,
                 clock=time.time):
        self.capacity = float(capacity)
        self.refill_per_sec = float(refill_per_sec)
        self._clock = clock
        self._client = client
        self._fallback = RateLimiter(capacity, refill_per_sec)

    @property
    def client(self):
        if self._client is None:
            from google.cloud import firestore

            self._client = firestore.Client()
        return self._client

    @staticmethod
    def doc_id(key: str) -> str:
        # Keys hold client addresses and ':' / '/'; a hash is a safe,
        # fixed-length document id that doesn't store the address in clear.
        return hashlib.sha256(key.encode()).hexdigest()[:40]

    def check(self, key: str) -> tuple[bool, float]:
        try:
            return self._check(key)
        except Exception:  # noqa: BLE001 - fail open to the local bucket
            logger.warning("Firestore rate limit unavailable; using the local bucket",
                           exc_info=True)
            return self._fallback.check(key)

    def _check(self, key: str) -> tuple[bool, float]:
        from datetime import UTC, datetime

        from google.cloud import firestore

        ref = self.client.collection(COLLECTION).document(self.doc_id(key))
        now = self._clock()

        @firestore.transactional
        def step(transaction) -> tuple[bool, float]:
            snap = ref.get(transaction=transaction)
            data = snap.to_dict() if snap.exists else None
            tokens = data["tokens"] if data else self.capacity
            updated = data["updated"] if data else now
            tokens, allowed, retry_after = take(
                tokens, updated, now, self.capacity, self.refill_per_sec
            )
            transaction.set(ref, {
                "tokens": tokens,
                "updated": now,
                "expires_at": datetime.fromtimestamp(now + _EXPIRE_AFTER_S, UTC),
            })
            return allowed, retry_after

        return step(self.client.transaction())

    def reset(self) -> None:
        """Local fallback only — never wipes the shared collection."""
        self._fallback.reset()
