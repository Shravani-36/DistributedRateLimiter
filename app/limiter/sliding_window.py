import uuid

from app.limiter.base import Decision, RateLimiter

# Runs inside Redis, so the trim / count / add sequence is atomic: two API
# instances can never both read "9 requests used" and both let a request in.
#
# The clock comes from Redis too, not from the API container. Every instance
# then measures the window against the same clock, so a few seconds of drift
# between hosts cannot let one instance think a request has already aged out
# while another still counts it.
SLIDING_WINDOW_LUA = """
local key    = KEYS[1]
local window = tonumber(ARGV[1])
local limit  = tonumber(ARGV[2])
local unique = ARGV[3]

-- Redis' own clock: [seconds, microseconds] -> milliseconds
local t   = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)

-- 1. forget everything older than the window
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)

-- 2. how many requests are still inside the window?
local count = redis.call('ZCARD', key)

if count < limit then
    -- 3. record this request and keep the key self-cleaning.
    -- The member must be unique or ZADD would overwrite an earlier request
    -- that landed in the same millisecond, silently undercounting.
    redis.call('ZADD', key, now, string.format('%d-%s', now, unique))
    redis.call('PEXPIRE', key, window)
    return {1, limit - count - 1, 0}
end

-- over the limit: the client may retry once the oldest request falls out
local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
local retry_after_ms = window
if oldest[2] then
    retry_after_ms = math.ceil(tonumber(oldest[2]) + window - now)
end
return {0, 0, retry_after_ms}
"""


class SlidingWindowLimiter(RateLimiter):
    """Keeps a log of request timestamps in a Redis sorted set.

    Unlike the fixed window, the window moves with the clock: it always looks
    at the last `window` seconds, so the boundary burst (limit requests at
    0:59 plus limit more at 1:01) is rejected.

    Cost: one sorted-set member per request inside the window, versus a single
    integer for the fixed window.
    """

    def __init__(self, redis, limit: int, window: int):
        self.redis = redis
        self.limit = limit
        self.window_ms = window * 1000
        self._script = redis.register_script(SLIDING_WINDOW_LUA)

    def _key(self, client_id: str) -> str:
        return f"rl:sliding:{client_id}"

    def allow(self, client_id: str) -> Decision:
        allowed, remaining, retry_after_ms = self._script(
            keys=[self._key(client_id)],
            args=[self.window_ms, self.limit, uuid.uuid4().hex],
        )

        return Decision(
            allowed=bool(allowed),
            limit=self.limit,
            remaining=int(remaining),
            retry_after=max(1, -(-int(retry_after_ms) // 1000)) if not allowed else 0,
        )
