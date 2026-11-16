"""Request accounting for runs on a metered or free-tier quota.

The first Phase 4 run spent $5 of Anthropic credit and then failed 113 clauses
with "credit balance is too low" — the run did not stop, it degraded, and the
resulting F1 was computed over a half-finished experiment. A budget that raises
*before* the call that would exceed it turns that into a deliberate stop: the
report then says 70 of 419 attempted rather than 419 attempted and 229 broken.

`rpm` paces calls against a sliding one-minute window, because a free tier
rejects bursts even when the daily total is nowhere near its cap.
"""

import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from core.logging import get_logger

log = get_logger("core.budget")

_WINDOW_SECONDS = 60.0


class BudgetExceeded(RuntimeError):
    """The next call would exceed the run's request allowance."""


class LimiterClosed(BudgetExceeded):
    """The limiter was closed (server shutdown) while a request waited or asked for room.

    A subclass of BudgetExceeded so every caller that already lets a budget stop
    propagate (core.llm) lets this one through too, instead of retrying it.
    """


@dataclass
class RequestBudget:
    """Counts and paces provider requests. Safe to share across worker threads.

    The harness runs clauses concurrently, so both the counter and the pacing
    window are guarded by one lock. `reserve` is called before the request, not
    after, so the cap is never crossed even by a call that then fails.
    """

    max_requests: int | None = None
    rpm: int | None = None
    _spent: int = field(default=0, init=False)
    _recent: list[float] = field(default_factory=list, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def spent(self) -> int:
        with self._lock:
            return self._spent

    def remaining(self) -> int | None:
        if self.max_requests is None:
            return None
        with self._lock:
            return max(0, self.max_requests - self._spent)

    def reserve(self, n: int = 1) -> None:
        """Claim `n` requests, waiting for the rate window if needed.

        Raises BudgetExceeded rather than sleeping past the cap: a run that has
        run out of allowance should fail loudly now, not quietly later.
        """
        if n < 1:
            raise ValueError(f"cannot reserve {n} requests")

        while True:
            with self._lock:
                if self.max_requests is not None and self._spent + n > self.max_requests:
                    raise BudgetExceeded(
                        f"{self._spent} of {self.max_requests} requests spent; "
                        f"{n} more would exceed the run budget"
                    )

                wait = self._wait_for_window(n)
                if wait <= 0:
                    now = time.monotonic()
                    self._recent.extend([now] * n)
                    self._spent += n
                    return

            # Sleep outside the lock so other workers can still be accounted.
            log.debug("rate window full, waiting %.2fs", wait)
            time.sleep(wait)

    def _wait_for_window(self, n: int) -> float:
        """Seconds to wait before `n` more calls fit the rpm window. Caller holds the lock."""
        if self.rpm is None:
            return 0.0

        now = time.monotonic()
        cutoff = now - _WINDOW_SECONDS
        self._recent = [stamp for stamp in self._recent if stamp > cutoff]

        if len(self._recent) + n <= self.rpm:
            return 0.0

        # The call that frees a slot is the one leaving the window soonest.
        index = len(self._recent) + n - self.rpm - 1
        return max(0.0, self._recent[index] + _WINDOW_SECONDS - now)


def from_config(run: dict) -> RequestBudget:
    """Build a budget from the `run` block of configs/evals.yaml.

    `rpm` is the committed key name; `requests_per_minute` is accepted as an
    alias. Reading only the alias is how this silently yielded an unpaced
    budget: the key validation in evals/run_baseline.py passed, because the
    config really did carry `rpm`, while the pacing it configures never
    happened. A missing rpm cannot be distinguished from "no pacing wanted",
    so the mismatch produces no error at all - just 429s at full concurrency.
    """
    return RequestBudget(
        max_requests=run.get("request_budget"),
        rpm=run.get("rpm", run.get("requests_per_minute")),
    )


class SharedLimiter:
    """One rate limit and one daily request budget for the whole server (Phase 5 contract).

    `RequestBudget` is per run: two reviews at once would each pace themselves
    to the full rpm and together exceed the provider's limit. The service builds
    exactly one `SharedLimiter` at startup and hands it to every review.

    Contract:
    - `acquire(n=1, *, timeout=None)` blocks until `n` requests fit both the rpm
      window and today's budget (UTC day), then claims them. Out of daily budget
      means waiting for the next day (or `timeout`, then `BudgetExceeded`), never
      a silent failure.
    - `waiting_reason()` -> "rate_limit" | "daily_budget" | None, for the UI.
    - `remaining_today()` -> int | None.
    - `close()` wakes every waiter at once; it and every later `acquire` raise
      `LimiterClosed`. A wait until midnight must not hold up a server shutdown,
      so the default sleep is an `Event.wait` that `close()` interrupts.
    - `clock` and `sleep` are injectable so tests run without real time; an
      injected sleep is not interrupted, but the closed check follows it.
    - Thread-safe; sleeping happens outside the lock.
    """

    def __init__(
        self,
        *,
        rpm: int | None = None,
        daily_budget: int | None = None,
        clock=time.time,
        sleep=None,
    ) -> None:
        if rpm is not None and rpm < 1:
            raise ValueError("rpm must be positive")
        if daily_budget is not None and daily_budget < 1:
            raise ValueError("daily_budget must be positive")
        self.rpm = rpm
        self.daily_budget = daily_budget
        self._clock = clock
        self._closed = threading.Event()
        self._sleep = sleep if sleep is not None else self._closed.wait
        self._recent: list[float] = []
        self._day: str | None = None
        self._spent_today = 0
        self._waiting_reason: str | None = None
        self._lock = threading.Lock()

    def acquire(self, n: int = 1, *, timeout: float | None = None) -> None:
        """Reserve requests after both shared limits allow them."""
        if n < 1:
            raise ValueError(f"cannot acquire {n} requests")
        if self.rpm is not None and n > self.rpm:
            raise ValueError(f"cannot acquire {n} requests with rpm={self.rpm}")
        if self.daily_budget is not None and n > self.daily_budget:
            raise ValueError(f"cannot acquire {n} requests with daily_budget={self.daily_budget}")
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must not be negative")

        started = self._clock()
        while True:
            with self._lock:
                if self._closed.is_set():
                    self._waiting_reason = None
                    raise LimiterClosed("the limiter is closed: the server is shutting down")
                now = self._clock()
                self._roll_day(now)
                reason, wait = self._wait_for(n, now)
                if reason is None:
                    self._recent.extend([now] * n)
                    self._spent_today += n
                    self._waiting_reason = None
                    return

                elapsed = now - started
                if timeout is not None and elapsed + wait > timeout:
                    self._waiting_reason = None
                    raise BudgetExceeded(f"timed out waiting for {reason}")
                self._waiting_reason = reason

            # Do not hold the accounting lock while a review waits.
            self._sleep(wait)

    def reserve(self, n: int = 1) -> None:
        """Offer the RequestBudget protocol used by core.llm."""
        self.acquire(n)

    def close(self) -> None:
        """Wake every waiting request and refuse new ones (LimiterClosed). Idempotent."""
        self._closed.set()
        with self._lock:
            self._waiting_reason = None

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def waiting_reason(self) -> str | None:
        with self._lock:
            return self._waiting_reason

    def remaining_today(self) -> int | None:
        if self.daily_budget is None:
            return None
        with self._lock:
            self._roll_day(self._clock())
            return max(0, self.daily_budget - self._spent_today)

    def _roll_day(self, now: float) -> None:
        day = datetime.fromtimestamp(now, UTC).date().isoformat()
        if day != self._day:
            self._day = day
            self._spent_today = 0

    def _wait_for(self, n: int, now: float) -> tuple[str | None, float]:
        if self.daily_budget is not None and self._spent_today + n > self.daily_budget:
            current = datetime.fromtimestamp(now, UTC)
            tomorrow = datetime.combine(
                current.date() + timedelta(days=1), datetime.min.time(), UTC
            )
            return "daily_budget", max(0.0, tomorrow.timestamp() - now)

        if self.rpm is None:
            return None, 0.0
        cutoff = now - _WINDOW_SECONDS
        self._recent = [stamp for stamp in self._recent if stamp > cutoff]
        if len(self._recent) + n <= self.rpm:
            return None, 0.0
        index = len(self._recent) + n - self.rpm - 1
        return "rate_limit", max(0.0, self._recent[index] + _WINDOW_SECONDS - now)
