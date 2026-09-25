from app.config import settings
from app.limiter.base import Decision, RateLimiter
from app.limiter.fixed_window import FixedWindowLimiter
from app.limiter.resilient import ResilientLimiter
from app.limiter.sliding_window import SlidingWindowLimiter

__all__ = [
    "Decision",
    "RateLimiter",
    "FixedWindowLimiter",
    "SlidingWindowLimiter",
    "ResilientLimiter",
    "build_limiter",
]

ALGORITHMS = {
    "fixed": FixedWindowLimiter,
    "sliding": SlidingWindowLimiter,
}


def build_limiter(redis, algorithm: str | None = None) -> RateLimiter:
    """Pick the algorithm by name (settings.algorithm by default)."""
    name = (algorithm or settings.algorithm).lower()
    try:
        limiter_cls = ALGORITHMS[name]
    except KeyError:
        raise ValueError(
            f"Unknown rate limit algorithm {name!r}, expected one of {sorted(ALGORITHMS)}"
        ) from None

    limiter = limiter_cls(redis, settings.rate_limit, settings.window_seconds)
    # Every limiter talks to Redis, so every limiter needs an outage policy.
    return ResilientLimiter(limiter, settings.fail_open, settings.rate_limit)
