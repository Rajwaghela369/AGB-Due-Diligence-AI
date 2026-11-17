"""Cost and time figures for the UI (Phase 8, 8.D), derived from stored rows and config.

Nothing here is stored: the verdicts keep their tokens and latency, the review keeps
`cost_usd` and its timestamps, and the prices live in `configs/app.yaml`. Deriving the
money and the wall time in one place keeps the header, the home list and the clause
details on the same numbers.
"""

from datetime import UTC, datetime
from typing import Any

from app.api.deps import setting
from app.api.schemas import Pricing
from app.domain import IDLE_REVIEW_STATUSES, Review


def pricing(settings: Any) -> Pricing:
    """`review.price_usd_per_million_tokens` and `review.eur_per_usd`; absent keys are 0
    and None, and `confirmed` is False until a price is set."""
    prices = setting(settings, "review.price_usd_per_million_tokens", None) or {}
    input_price = float(prices.get("input", 0.0) or 0.0)
    output_price = float(prices.get("output", 0.0) or 0.0)
    rate = setting(settings, "review.eur_per_usd", None)
    return Pricing(
        input_usd_per_million=input_price,
        output_usd_per_million=output_price,
        eur_per_usd=float(rate) if rate is not None else None,
        confirmed=input_price > 0 or output_price > 0,
    )


def cache_hit_rate(review: Review) -> float:
    counts = review.counts
    return counts.cache_hits / counts.total if counts.total else 0.0


def cost_eur(review: Review, prices: Pricing) -> float | None:
    return review.cost_usd * prices.eur_per_usd if prices.eur_per_usd is not None else None


def wall_time_ms(review: Review, now: datetime | None = None) -> int | None:
    """From the first start to the end of the last run; to now while a run is going."""
    if review.started_at is None:
        return None
    if review.status in IDLE_REVIEW_STATUSES and review.finished_at is not None:
        end = review.finished_at
    else:
        end = now or datetime.now(UTC)
    start = review.started_at
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    return max(0, round((end - start).total_seconds() * 1000))
