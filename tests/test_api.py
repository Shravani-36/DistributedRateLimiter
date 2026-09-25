import fakeredis
import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError

from app import main
from app.limiter.fixed_window import FixedWindowLimiter
from app.limiter.resilient import ResilientLimiter


class _BrokenLimiter:
    """Stands in for a limiter whose Redis has gone away."""

    def allow(self, client_id):
        raise RedisConnectionError("Connection refused")


class _BrokenRedis:
    def ping(self):
        raise RedisConnectionError("Connection refused")


def _kill_redis(monkeypatch):
    """Take Redis away from both the limiter and the health check."""
    main.limiter.inner = _BrokenLimiter()
    monkeypatch.setattr(main, "redis_client", _BrokenRedis())


@pytest.fixture
def client(monkeypatch):
    """Swap the real Redis-backed limiter for an in-memory one.

    Wrapped in ResilientLimiter exactly like build_limiter does, so these
    tests exercise the same object shape production uses.
    """
    fake = fakeredis.FakeRedis(decode_responses=True)
    limiter = ResilientLimiter(
        FixedWindowLimiter(fake, limit=3, window=60), fail_open=True, limit=3
    )
    monkeypatch.setattr(main, "limiter", limiter)
    return TestClient(main.app)


def test_allowed_requests_carry_rate_limit_headers(client):
    res = client.get("/api/data", headers={"X-API-Key": "user1"})
    assert res.status_code == 200
    assert res.headers["X-RateLimit-Limit"] == "3"
    assert res.headers["X-RateLimit-Remaining"] == "2"


def test_fourth_request_is_rejected_with_429(client):
    headers = {"X-API-Key": "user1"}
    codes = [client.get("/api/data", headers=headers).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def test_429_tells_the_client_when_to_retry(client):
    headers = {"X-API-Key": "user1"}
    for _ in range(3):
        client.get("/api/data", headers=headers)
    res = client.get("/api/data", headers=headers)
    assert res.status_code == 429
    assert int(res.headers["Retry-After"]) >= 1


def test_different_api_keys_get_their_own_budget(client):
    for _ in range(3):
        client.get("/api/data", headers={"X-API-Key": "user1"})
    assert client.get("/api/data", headers={"X-API-Key": "user2"}).status_code == 200


def test_health_is_never_rate_limited(client):
    for _ in range(10):
        assert client.get("/health").status_code == 200


def test_requests_are_served_and_marked_when_redis_is_down(client, monkeypatch):
    """Fail-open: the API keeps working, but says the limit was not checked."""
    _kill_redis(monkeypatch)
    res = client.get("/api/data", headers={"X-API-Key": "user1"})
    assert res.status_code == 200
    assert res.headers["X-RateLimit-Degraded"] == "true"
    assert res.headers["X-RateLimit-Remaining"] == "-1"  # unknown, not zero


def test_health_reports_degraded_but_stays_ready_when_failing_open(client, monkeypatch):
    _kill_redis(monkeypatch)
    res = client.get("/health")
    assert res.status_code == 200  # still able to serve traffic
    body = res.json()
    assert body["redis"] is False
    assert body["status"] == "degraded"
    assert body["policy"] == "fail_open"


def test_health_reports_not_ready_when_failing_closed(client, monkeypatch):
    """Failing closed, the instance can only produce 429s, so it should be
    pulled out of the load balancer rotation."""
    monkeypatch.setattr(main.settings, "fail_open", False)
    _kill_redis(monkeypatch)
    res = client.get("/health")
    assert res.status_code == 503
    assert res.json()["policy"] == "fail_closed"
