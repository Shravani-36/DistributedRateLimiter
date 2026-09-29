from app.limiter.base import Decision, RateLimiter

# The bucket number is derived from the clock, so which bucket a request lands
# in used to depend on whichever API container happened to serve it. Two
# instances a second apart could disagree about where the minute starts and
# hand out two separate allowances. Taking the time from Redis removes that:
# the bucket is computed once, in one place, from one clock.
#
# Note: the key is built inside the script rather than passed in, because it
# depends on the time we have just read. That assumes a single Redis (which is
# what this project runs); under Redis Cluster every key a script touches has
# to be declared in KEYS so the slot can be checked up front.
FIXED_WINDOW_LUA = """
local prefix = KEYS[1]
local window = tonumber(ARGV[1])
local limit  = tonumber(ARGV[2])

local t   = redis.call('TIME')
local now = tonumber(t[1])

local bucket = math.floor(now / window)
local key    = string.format('%s:%d', prefix, bucket)

local count = redis.call('INCR', key)
if count == 1 then
    -- Only on creation. Refreshing the TTL on every request would keep a
    -- finished bucket alive long after its minute is over.
    redis.call('EXPIRE', key, window)
end

local allowed = 0
if count <= limit then
    allowed = 1
end

-- the whole bucket is spent until the clock rolls into the next one
local retry_after = window - (now % window)

return {allowed, math.max(0, limit - count), retry_after}
"""


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
        self._script = redis.register_script(FIXED_WINDOW_LUA)

    def _key(self, client_id: str) -> str:
        return f"rl:fixed:{client_id}"

    def allow(self, client_id: str) -> Decision:
        allowed, remaining, retry_after = self._script(
            keys=[self._key(client_id)],
            args=[self.window, self.limit],
        )

        return Decision(
            allowed=bool(allowed),
            limit=self.limit,
            remaining=int(remaining),
            retry_after=max(1, int(retry_after)),
        )
