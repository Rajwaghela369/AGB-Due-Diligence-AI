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

from core.logging import get_logger

log = get_logger("core.budget")

_WINDOW_SECONDS = 60.0


class BudgetExceeded(RuntimeError):
    """The next call would exceed the run's request allowance."""


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
    - `clock` and `sleep` are injectable so tests run without real time.
    - Thread-safe; sleeping happens outside the lock.
    """

    def __init__(
        self,
        *,
        rpm: int | None = None,
        daily_budget: int | None = None,
        clock=time.time,
        sleep=time.sleep,
    ) -> None:
        raise NotImplementedError("Phase 5 Wave 1: core-hooks")
