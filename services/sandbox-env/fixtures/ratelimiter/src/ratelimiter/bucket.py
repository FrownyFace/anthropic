"""Token bucket: `capacity` tokens max, refilled continuously at `refill_per_sec`."""

import time


class TokenBucket:
    def __init__(self, capacity: int, refill_per_sec: float, clock=time.monotonic):
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if refill_per_sec < 0:
            raise ValueError("refill_per_sec must be >= 0")
        self.capacity = capacity
        self.refill_per_sec = refill_per_sec
        self._clock = clock
        self._tokens = float(capacity)
        self._last = clock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._last)
        self._last = now
        self._tokens = min(float(self.capacity), self._tokens + elapsed * self.refill_per_sec)

    @property
    def tokens(self) -> float:
        self._refill()
        return self._tokens

    def try_acquire(self, n: int = 1) -> bool:
        if n <= 0:
            raise ValueError("n must be positive")
        self._refill()
        if self._tokens >= n:
            self._tokens -= n
            return True
        return False
