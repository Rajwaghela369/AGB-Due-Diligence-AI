"""The server's one request limiter, and the other review settings, from `configs/app.yaml`.

Exactly one `SharedLimiter` exists per process and every review's judge
reserves from it: two limiters would each pace themselves to the provider's
full rpm and together exceed it. The API builds it at startup, hands it to
`make_judge` and to `build_service`, and nothing else constructs one.
"""

import time
from typing import Any

from core.budget import SharedLimiter

DEFAULT_MAX_CONCURRENCY = 4


def _review_block(app_config: dict[str, Any] | None) -> dict[str, Any]:
    if app_config is None:
        from core.config import load_yaml
        from core.paths import CONFIGS

        app_config = load_yaml(CONFIGS / "app.yaml")
    return dict(app_config.get("review") or {})


def build_limiter(
    app_config: dict[str, Any] | None = None, *, clock=time.time, sleep=time.sleep
) -> SharedLimiter:
    """`review.rpm` and `review.daily_budget`; either may be absent for no limit."""
    review = _review_block(app_config)
    return SharedLimiter(
        rpm=review.get("rpm"),
        daily_budget=review.get("daily_budget"),
        clock=clock,
        sleep=sleep,
    )


def review_settings(app_config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Keyword arguments for `build_service` from the `review` block.

    `prices` is USD per million tokens, `{"input": x, "output": y}`; both
    default to 0 so cost reads 0 rather than a wrong number until it is set.
    """
    review = _review_block(app_config)
    prices = review.get("price_usd_per_million_tokens") or {}
    return {
        "max_concurrency": int(review.get("max_concurrency", DEFAULT_MAX_CONCURRENCY)),
        "prices": {
            "input": float(prices.get("input", 0.0)),
            "output": float(prices.get("output", 0.0)),
        },
    }
