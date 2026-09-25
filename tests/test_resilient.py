import fakeredis
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.limiter.resilient import ResilientLimiter
from app.limiter.sliding_window import SlidingWindowLimiter


class BrokenLimiter:
    """Stands in for a limiter whose Redis has gone away."""

    def allow(self, client_id):
        raise RedisConnectionError("Connection refused")


def test_fail_open_serves_traffic_when_redis_is_down():
    limiter = ResilientLimiter(BrokenLimiter(), fail_open=True, limit=10)
    decision = limiter.allow("u1")
    assert decision.allowed
    assert decision.degraded
    assert decision.remaining == -1  # unknown, not zero


def test_fail_closed_rejects_when_redis_is_down():
    limiter = ResilientLimiter(BrokenLimiter(), fail_open=False, limit=10)
    decision = limiter.allow("u1")
    assert not decision.allowed
    assert decision.degraded
    assert decision.retry_after >= 1


def test_outages_are_counted_not_swallowed():
    limiter = ResilientLimiter(BrokenLimiter(), fail_open=True, limit=10)
    for _ in range(3):
        limiter.allow("u1")
    assert limiter.redis_errors == 3


def test_healthy_redis_is_untouched_by_the_wrapper():
    redis = fakeredis.FakeRedis(decode_responses=True)
    inner = SlidingWindowLimiter(redis, limit=2, window=60)
    limiter = ResilientLimiter(inner, fail_open=True, limit=2)

    assert [limiter.allow("u1").allowed for _ in range(3)] == [True, True, False]
    assert limiter.redis_errors == 0
    assert not limiter.allow("u1").degraded


def test_limits_resume_once_redis_recovers():
    """The failure must not be sticky: as soon as Redis answers again the
    real counters take over."""
    redis = fakeredis.FakeRedis(decode_responses=True)
    inner = SlidingWindowLimiter(redis, limit=1, window=60)
    limiter = ResilientLimiter(inner, fail_open=True, limit=1)

    limiter.inner = BrokenLimiter()
    assert limiter.allow("u1").degraded  # outage: served blind

    limiter.inner = inner  # Redis comes back
    assert limiter.allow("u1").allowed  # the one real slot
    assert not limiter.allow("u1").allowed  # and the limit bites again


def test_non_redis_errors_still_propagate():
    """Only Redis failures are absorbed. A bug in the limiter must not be
    silently turned into 'allowed'."""

    class BuggyLimiter:
        def allow(self, client_id):
            raise ValueError("boom")

    limiter = ResilientLimiter(BuggyLimiter(), fail_open=True, limit=10)
    with pytest.raises(ValueError):
        limiter.allow("u1")
