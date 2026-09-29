from app.limiter.sliding_window import SlidingWindowLimiter
from app.limiter.token_bucket import TokenBucketLimiter


def test_a_new_client_starts_with_a_full_bucket(redis):
    limiter = TokenBucketLimiter(redis, limit=5, window=60)
    assert [limiter.allow("u1").allowed for _ in range(5)] == [True] * 5


def test_blocks_once_the_bucket_is_empty(redis):
    limiter = TokenBucketLimiter(redis, limit=3, window=60)
    allowed = [limiter.allow("u1").allowed for _ in range(5)]
    assert allowed == [True, True, True, False, False]


def test_remaining_counts_down(redis):
    limiter = TokenBucketLimiter(redis, limit=3, window=60)
    assert [limiter.allow("u1").remaining for _ in range(4)] == [2, 1, 0, 0]


def test_clients_are_independent(redis):
    limiter = TokenBucketLimiter(redis, limit=1, window=60)
    assert limiter.allow("u1").allowed
    assert limiter.allow("u2").allowed
    assert not limiter.allow("u1").allowed


def test_two_instances_share_one_bucket(redis):
    """The point of the whole project: one budget, not one per instance."""
    api1 = TokenBucketLimiter(redis, limit=2, window=60)
    api2 = TokenBucketLimiter(redis, limit=2, window=60)
    assert api1.allow("u1").allowed
    assert api2.allow("u1").allowed
    assert not api1.allow("u1").allowed


def test_tokens_refill_over_time(redis, clock):
    # 6 per 60s = one token every 10 seconds
    limiter = TokenBucketLimiter(redis, limit=6, window=60)
    for _ in range(6):
        limiter.allow("u1")
    assert not limiter.allow("u1").allowed

    clock.advance(10)  # exactly one token's worth
    assert limiter.allow("u1").allowed
    assert not limiter.allow("u1").allowed  # and only one

    clock.advance(30)  # three more
    assert [limiter.allow("u1").allowed for _ in range(4)] == [
        True,
        True,
        True,
        False,
    ]


def test_refill_never_exceeds_capacity(redis, clock):
    """An idle client banks a burst, but not an unlimited one."""
    limiter = TokenBucketLimiter(redis, limit=5, window=60)
    limiter.allow("u1")

    clock.advance(6000)  # idle for 100 windows

    # Still only 5, not 500.
    assert [limiter.allow("u1").allowed for _ in range(6)] == [True] * 5 + [False]


def test_rejected_requests_do_not_spend_tokens(redis, clock):
    """Hammering while empty must not push the refill further away."""
    limiter = TokenBucketLimiter(redis, limit=2, window=60)
    for _ in range(2):
        limiter.allow("u1")
    for _ in range(50):  # 50 rejections
        assert not limiter.allow("u1").allowed

    clock.advance(30)  # one token's worth at 2/60s
    assert limiter.allow("u1").allowed


def test_burst_can_exceed_the_sustained_rate(redis, clock):
    """What the window algorithms cannot express: spike now, pay for it later."""
    limiter = TokenBucketLimiter(redis, limit=6, window=60, burst=12)

    # Twelve at once, double the sustained allowance.
    assert [limiter.allow("u1").allowed for _ in range(12)] == [True] * 12
    assert not limiter.allow("u1").allowed

    # But refill is still the sustained rate: one per 10s, not one per 5s.
    clock.advance(10)
    assert limiter.allow("u1").allowed
    assert not limiter.allow("u1").allowed


def test_retry_after_points_at_the_next_token(redis):
    limiter = TokenBucketLimiter(redis, limit=6, window=60)  # a token every 10s
    for _ in range(6):
        limiter.allow("u1")

    decision = limiter.allow("u1")
    assert not decision.allowed
    assert 1 <= decision.retry_after <= 10


def test_paces_a_busy_client_where_the_sliding_window_clumps(redis, clock):
    """The behavioural difference worth knowing in an interview.

    Both allow the same number over time. The sliding window hands the whole
    allowance back in one lump when the oldest requests age out; the bucket
    drips it back one token at a time.
    """
    bucket = TokenBucketLimiter(redis, limit=4, window=60)
    sliding = SlidingWindowLimiter(redis, limit=4, window=60)

    for _ in range(4):
        bucket.allow("u1")
        sliding.allow("u1")

    # Three quarters of the way through the window.
    clock.advance(45)

    bucket_allowed = sum(bucket.allow("u1").allowed for _ in range(4))
    sliding_allowed = sum(sliding.allow("u1").allowed for _ in range(4))

    assert bucket_allowed == 3  # 45s at one token per 15s
    assert sliding_allowed == 0  # nothing has aged out of the window yet
