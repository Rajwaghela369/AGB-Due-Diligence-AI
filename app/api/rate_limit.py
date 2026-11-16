"""A process-local token bucket protects the two routes that start expensive work."""

import threading
import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    updated: float


class IpRateLimiter:
    def __init__(self, *, capacity: int, per_minute: int, clock=time.monotonic) -> None:
        if capacity < 1 or per_minute < 1:
            raise ValueError("rate limit values must be positive")
        self.capacity = capacity
        self.refill_per_second = per_minute / 60
        self.clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def allow(self, address: str) -> bool:
        now = self.clock()
        with self._lock:
            bucket = self._buckets.setdefault(address, _Bucket(float(self.capacity), now))
            elapsed = max(0.0, now - bucket.updated)
            bucket.tokens = min(self.capacity, bucket.tokens + elapsed * self.refill_per_second)
            bucket.updated = now
            if bucket.tokens < 1:
                return False
            bucket.tokens -= 1
            return True
