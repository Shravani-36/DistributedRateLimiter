import fakeredis
import pytest

from app.limiter.fixed_window import FixedWindowLimiter


@pytest.fixture
def redis():
    return fakeredis.FakeRedis(decode_responses=True)


def test_blocks_once_the_limit_is_reached(redis):
    limiter = FixedWindowLimiter(redis, limit=3, window=60)
    allowed = [limiter.allow("u1").allowed for _ in range(5)]
    assert allowed == [True, True, True, False, False]


def test_remaining_counts_down(redis):
    limiter = FixedWindowLimiter(redis, limit=3, window=60)
    assert [limiter.allow("u1").remaining for _ in range(4)] == [2, 1, 0, 0]


def test_clients_are_independent(redis):
    limiter = FixedWindowLimiter(redis, limit=1, window=60)
    assert limiter.allow("u1").allowed
    assert limiter.allow("u2").allowed
    assert not limiter.allow("u1").allowed


def test_two_instances_share_one_budget(redis):
    """The whole point: separate API servers, one shared Redis, one limit."""
    api1 = FixedWindowLimiter(redis, limit=2, window=60)
    api2 = FixedWindowLimiter(redis, limit=2, window=60)
    assert api1.allow("u1").allowed
    assert api2.allow("u1").allowed
    assert not api1.allow("u1").allowed


def test_counter_key_expires(redis):
    limiter = FixedWindowLimiter(redis, limit=5, window=60)
    limiter.allow("u1")
    key = next(iter(redis.scan_iter("rl:fixed:u1:*")))
    assert 0 < redis.ttl(key) <= 60
