"""
Token-bucket rate limiting with per-endpoint weights.

Kalshi meters by cost, not by request count: a read is cheap and an order
mutation is expensive. A limiter that counts requests will therefore either
throttle reads pointlessly or let a burst of order mutations blow the budget —
so the bucket is drained by the weight of the specific call.

Weights are configurable because Kalshi has revised them, and a hardcoded
weight that is too low is worse than no limiter at all: it produces confident
pacing that quietly exceeds the real budget and earns a 429 under exactly the
load where you least want one.

The bucket refills continuously rather than in discrete windows. Discrete
windows let a caller spend a full budget at the end of one window and again at
the start of the next, producing a burst of double the intended rate right at
the boundary — which is the shape of traffic that trips server-side limiters.

Thread- and coroutine-safe: `acquire` is async and serialises waiters through
a lock, so concurrent scanners cannot each observe the same free capacity and
both spend it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Final

logger = logging.getLogger(__name__)

# Endpoint weights in tokens. Reads are 1; V2 order mutations are the
# expensive path. Verify against Kalshi's current published limits before
# relying on these for anything sized.
DEFAULT_WEIGHTS: Final[dict[str, int]] = {
    "read": 1,
    "order_create": 15,
    "order_amend": 15,
    "order_decrease": 15,
    "order_cancel": 15,
    "order_batch_create": 15,
    "order_batch_cancel": 15,
}


class RateLimitExceeded(RuntimeError):
    """Raised when a call cannot be admitted within its timeout."""


@dataclass
class TokenBucket:
    """
    Continuously-refilling token bucket.

    capacity  – burst size, in tokens
    refill_per_second – sustained rate
    """

    capacity: float
    refill_per_second: float
    _tokens: float = field(init=False)
    _last: float = field(init=False)
    _lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        if self.capacity <= 0 or self.refill_per_second <= 0:
            raise ValueError("capacity and refill_per_second must be positive")
        self._tokens = float(self.capacity)
        self._last = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_per_second)
            self._last = now

    @property
    def available(self) -> float:
        self._refill()
        return self._tokens

    async def acquire(self, weight: int = 1, *, timeout: float | None = 30.0) -> float:
        """
        Wait until `weight` tokens are available, then spend them.

        Returns the seconds spent waiting, so callers can log or alert on
        sustained throttling. Raises RateLimitExceeded if the wait would exceed
        `timeout`, and ValueError if the weight can never fit — a request
        larger than the bucket would otherwise wait forever.
        """
        if weight <= 0:
            return 0.0
        if weight > self.capacity:
            raise ValueError(
                f"weight {weight} exceeds bucket capacity {self.capacity}; "
                "raise capacity or split the call"
            )

        started = time.monotonic()
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= weight:
                    self._tokens -= weight
                    return time.monotonic() - started

                deficit = weight - self._tokens
                wait = deficit / self.refill_per_second
                if timeout is not None and (time.monotonic() - started) + wait > timeout:
                    raise RateLimitExceeded(
                        f"need {weight} tokens, {self._tokens:.2f} available; "
                        f"wait {wait:.2f}s would exceed timeout {timeout}s"
                    )
                await asyncio.sleep(wait)


class WeightedLimiter:
    """A bucket plus the endpoint-weight table, keyed by operation name."""

    def __init__(
        self,
        *,
        capacity: float,
        refill_per_second: float,
        weights: dict[str, int] | None = None,
    ) -> None:
        self._bucket = TokenBucket(capacity=capacity, refill_per_second=refill_per_second)
        self._weights = dict(DEFAULT_WEIGHTS if weights is None else weights)
        self._throttled_seconds = 0.0

    @property
    def available(self) -> float:
        return self._bucket.available

    @property
    def throttled_seconds(self) -> float:
        """Cumulative time spent waiting — a rising number means undersized."""
        return self._throttled_seconds

    def weight_for(self, operation: str) -> int:
        """
        Unknown operations get the most expensive known weight, not 1.

        Failing safe matters here: a typo'd operation name that costs 1 token
        instead of 15 produces a limiter that under-counts precisely on the
        mutation path.
        """
        if operation in self._weights:
            return self._weights[operation]
        fallback = max(self._weights.values())
        logger.warning(
            "Unknown rate-limit operation %r; charging max weight %d", operation, fallback
        )
        return fallback

    async def acquire(self, operation: str, *, timeout: float | None = 30.0) -> float:
        waited = await self._bucket.acquire(self.weight_for(operation), timeout=timeout)
        if waited > 0:
            self._throttled_seconds += waited
            logger.debug("Rate limiter delayed %s by %.3fs", operation, waited)
        return waited
