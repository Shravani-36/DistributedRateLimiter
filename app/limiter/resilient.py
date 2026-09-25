import logging

from redis.exceptions import RedisError

from app.limiter.base import Decision, RateLimiter

logger = logging.getLogger(__name__)


class ResilientLimiter(RateLimiter):
    """Wraps a limiter and decides what to do when Redis is unreachable.

    Redis holds the shared counters, so if it goes down there is no way to
    know whether a client is over its limit. There is no correct answer, only
    a trade-off you have to choose up front:

    - fail_open=True  -> serve the request. Availability first. A Redis
      outage must not take the whole API down with it. The risk is that an
      abusive client is unlimited for the duration of the outage.
    - fail_open=False -> reject with 429. Correctness first. Use this when
      exceeding the limit is worse than being unavailable, e.g. a limit that
      guards a paid downstream API or a security boundary.

    Either way the outage is counted and logged, never silently swallowed.
    """

    def __init__(self, inner: RateLimiter, fail_open: bool, limit: int):
        self.inner = inner
        self.fail_open = fail_open
        self.limit = limit
        self.redis_errors = 0

    def allow(self, client_id: str) -> Decision:
        try:
            return self.inner.allow(client_id)
        except RedisError as exc:
            self.redis_errors += 1
            logger.warning(
                "Redis unavailable, failing %s: %s",
                "open" if self.fail_open else "closed",
                exc,
            )
            return Decision(
                allowed=self.fail_open,
                limit=self.limit,
                # -1 means "unknown", not "none left".
                remaining=-1,
                retry_after=0 if self.fail_open else 1,
                degraded=True,
            )
