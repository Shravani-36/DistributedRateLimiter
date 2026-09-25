import redis

from app.config import settings


def make_redis() -> redis.Redis:
    """Short timeouts: a slow Redis must never hang an API request."""
    return redis.Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=0.25,
        socket_connect_timeout=0.25,
    )


redis_client = make_redis()
