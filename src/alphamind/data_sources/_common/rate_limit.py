"""Per-provider continuous token-bucket rate limiter.

Thread-safe — ``acquire()`` may be called from multiple threads
simultaneously.  Each ``acquire()`` consumes one token.  When the bucket is
empty, ``acquire()`` blocks until enough time has elapsed for one token to
become available.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

__all__ = ["RateLimiter"]


@dataclass
class _BucketState:
    rate_per_second: float
    capacity: float
    tokens: float = field(init=False)
    last_refill: float = field(init=False)
    lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def __post_init__(self) -> None:
        self.tokens = self.capacity
        self.last_refill = time.monotonic()


class RateLimiter:
    """Per-provider continuous token-bucket rate limiter.

    Thread-safe — ``acquire()`` may be called from multiple threads
    simultaneously.  Each ``acquire()`` consumes one token.  When the
    bucket is empty, ``acquire()`` blocks until enough time has elapsed
    for one token to become available.
    """

    def __init__(self) -> None:
        self._buckets: dict[str, _BucketState] = {}
        self._registry_lock = threading.Lock()

    def set_limit(self, provider: str, *, rate_per_minute: int) -> None:
        """Register or update the rate limit for *provider*."""
        rate_per_second = rate_per_minute / 60.0
        capacity = float(rate_per_minute)
        with self._registry_lock:
            self._buckets[provider] = _BucketState(
                rate_per_second=rate_per_second,
                capacity=capacity,
            )

    def acquire(self, provider: str) -> None:
        """Block until one token is available for *provider*, then consume it.

        If *provider* has not been registered via :meth:`set_limit`, the call
        returns immediately (unlimited).
        """
        with self._registry_lock:
            bucket = self._buckets.get(provider)
        if bucket is None:
            return

        while True:
            with bucket.lock:
                now = time.monotonic()
                elapsed = now - bucket.last_refill
                # Refill proportionally to elapsed time
                bucket.tokens = min(
                    bucket.capacity,
                    bucket.tokens + elapsed * bucket.rate_per_second,
                )
                bucket.last_refill = now

                if bucket.tokens >= 1.0:
                    bucket.tokens -= 1.0
                    return

                # Calculate how long to wait for one token
                deficit = 1.0 - bucket.tokens
                wait = deficit / bucket.rate_per_second

            time.sleep(wait)
