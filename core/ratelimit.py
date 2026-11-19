import threading
import time
from collections.abc import Callable


class RateLimiter:
    """Token-bucket limiter capped at ``rpm`` requests per minute.

    ``rpm=None`` disables throttling (used for providers that are not RPM-limited).
    A burst of up to ``rpm`` requests is allowed, then requests are paced at
    ``rpm / 60`` per second. Safe to share between threads.
    """

    def __init__(
        self,
        rpm: float | None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._rate = (rpm / 60.0) if rpm else 0.0
        self._capacity = float(rpm) if rpm else 0.0
        self._tokens = self._capacity
        self._clock = clock
        self._sleeper = sleeper
        self._last = clock()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        if self._rate <= 0.0:
            return
        with self._lock:
            now = self._clock()
            self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
            self._last = now
            if self._tokens < 1.0:
                self._sleeper((1.0 - self._tokens) / self._rate)
                self._tokens = 1.0
                self._last = self._clock()
            self._tokens -= 1.0
