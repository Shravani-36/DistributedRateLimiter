import time

from app.limiter.base import Decision, RateLimiter


class FixedWindowLimiter(RateLimiter):
    """Time is chopped into buckets of `window` seconds.

    Every request does INCR on the bucket's counter. Once the counter passes
    `limit`, the rest of the bucket is rejected. Every API instance shares the
    same Redis key, so the limit holds across the whole cluster.

    Known weakness (fixed by the sliding window in phase 5): a client can send
    `limit` requests at the end of one bucket and `limit` more at the start of
    the next, so up to 2x the limit lands in a very short span.
    """

    def __init__(self, redis, limit: int, window: int):
        self.redis = redis
        self.limit = limit
        self.window = window

    def _key(self, client_id: str, bucket: int) -> str:
        return f"rl:fixed:{client_id}:{bucket}"

    def allow(self, client_id: str) -> Decision:
        now = time.time()
        bucket = int(now // self.window)
        key = self._key(client_id, bucket)

        # One round trip: increment, and make sure the key cleans itself up.
        pipe = self.redis.pipeline()
        pipe.incr(key)
        pipe.expire(key, self.window)
        count, _ = pipe.execute()

        seconds_into_window = now % self.window
        retry_after = max(1, int(self.window - seconds_into_window))

        return Decision(
            allowed=count <= self.limit,
            limit=self.limit,
            remaining=max(0, self.limit - count),
            retry_after=retry_after,
        )
