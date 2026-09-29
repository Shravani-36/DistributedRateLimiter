import math

from app.limiter.base import Decision, RateLimiter

# A bucket holds up to `capacity` tokens and refills at `rate` tokens/second.
# Every request spends one. An idle client builds a balance back up to
# capacity, which is what lets it burst; a busy one settles into the steady
# refill rate.
#
# Only two numbers are stored per client (tokens, timestamp) regardless of
# traffic, so unlike the sliding window the memory does not grow with load.
#
# The clock is Redis', not the caller's: the refill is computed from the gap
# between two Redis timestamps, so drift between API hosts cannot mint extra
# tokens.
TOKEN_BUCKET_LUA = """
local key      = KEYS[1]
local capacity = tonumber(ARGV[1])
local rate     = tonumber(ARGV[2])   -- tokens per second
local ttl      = tonumber(ARGV[3])   -- ms

local t   = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)

local state  = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts     = tonumber(state[2])

if tokens == nil or ts == nil then
    -- a client we have not seen starts with a full bucket
    tokens = capacity
    ts = now
end

-- pay in the tokens earned since the last request, never above capacity
local elapsed = math.max(0, now - ts) / 1000
tokens = math.min(capacity, tokens + elapsed * rate)

local allowed = 0
local retry_after_ms = 0
if tokens >= 1 then
    tokens = tokens - 1
    allowed = 1
else
    -- how long until one whole token has dripped back in
    retry_after_ms = math.ceil((1 - tokens) / rate * 1000)
end

-- Written on rejection too: no token was spent, but the refill clock still
-- has to move forward or the next request would be paid twice for the wait.
redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', key, ttl)

return {allowed, math.floor(tokens), retry_after_ms}
"""


class TokenBucketLimiter(RateLimiter):
    """Allows controlled bursts, then settles to a steady rate.

    Where the sliding window says "no more than `limit` in any 60 seconds",
    the token bucket says "spend up to `burst` at once, and earn more back at
    `limit/window` per second". A client that has been quiet can spike; one
    that is constantly at the limit is paced smoothly instead of being let
    through in a clump the moment the window rolls.

    That pacing is the practical difference: the sliding window frees its
    whole allowance at once when the oldest request ages out, while the bucket
    drips it back one token at a time.
    """

    def __init__(self, redis, limit: int, window: int, burst: int | None = None):
        self.redis = redis
        # Capacity is the burst. By default it matches the limit, so the
        # biggest spike equals the sustained allowance.
        self.capacity = burst or limit
        self.rate = limit / window  # tokens per second
        # Once a bucket has refilled to full it is indistinguishable from a
        # client we have never seen, so the key can be dropped at that point.
        self.ttl_ms = int(math.ceil(self.capacity / self.rate * 1000))
        self._script = redis.register_script(TOKEN_BUCKET_LUA)

    def _key(self, client_id: str) -> str:
        return f"rl:token:{client_id}"

    def allow(self, client_id: str) -> Decision:
        allowed, remaining, retry_after_ms = self._script(
            keys=[self._key(client_id)],
            args=[self.capacity, self.rate, self.ttl_ms],
        )

        return Decision(
            allowed=bool(allowed),
            limit=self.capacity,
            remaining=int(remaining),
            retry_after=max(1, -(-int(retry_after_ms) // 1000)) if not allowed else 0,
        )
