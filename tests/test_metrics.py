import fakeredis
import pytest
from fastapi.testclient import TestClient

from app import main
from app.limiter.fixed_window import FixedWindowLimiter
from app.limiter.resilient import ResilientLimiter


@pytest.fixture
def client(monkeypatch):
    fake = fakeredis.FakeRedis(decode_responses=True)
    limiter = ResilientLimiter(
        FixedWindowLimiter(fake, limit=2, window=60), fail_open=True, limit=2
    )
    monkeypatch.setattr(main, "limiter", limiter)
    return TestClient(main.app)


def _sample(body: str, name: str, **labels) -> float:
    """Pull one sample out of the Prometheus text exposition format."""
    label_bits = [f'{k}="{v}"' for k, v in labels.items()]
    for line in body.splitlines():
        if not line.startswith(name) or line.startswith("#"):
            continue
        if all(bit in line for bit in label_bits):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


def test_metrics_endpoint_is_exposed(client):
    res = client.get("/metrics")
    assert res.status_code == 200
    assert "rl_requests_total" in res.text


def test_allowed_and_blocked_are_counted_separately(client):
    before_allowed = _sample(
        client.get("/metrics").text, "rl_requests_total", result="allowed"
    )
    before_blocked = _sample(
        client.get("/metrics").text, "rl_requests_total", result="blocked"
    )

    for _ in range(5):  # limit is 2, so 2 allowed and 3 blocked
        client.get("/api/data", headers={"X-API-Key": "metrics-user"})

    body = client.get("/metrics").text
    assert _sample(body, "rl_requests_total", result="allowed") - before_allowed == 2
    assert _sample(body, "rl_requests_total", result="blocked") - before_blocked == 3


def test_check_duration_is_observed(client):
    client.get("/api/data", headers={"X-API-Key": "timed-user"})
    body = client.get("/metrics").text
    assert _sample(body, "rl_check_duration_seconds_count") > 0


def test_scraping_metrics_does_not_consume_the_limit(client):
    """A scrape every 5s must not eat a client's budget."""
    for _ in range(10):
        client.get("/metrics")
    # The full limit of 2 is still available.
    codes = [
        client.get("/api/data", headers={"X-API-Key": "untouched"}).status_code
        for _ in range(3)
    ]
    assert codes == [200, 200, 429]


def test_redis_outage_shows_up_in_metrics(client, monkeypatch):
    class _BrokenLimiter:
        def allow(self, client_id):
            from redis.exceptions import ConnectionError as RedisConnectionError

            raise RedisConnectionError("Connection refused")

    before = _sample(client.get("/metrics").text, "rl_redis_errors_total")
    main.limiter.inner = _BrokenLimiter()
    client.get("/api/data", headers={"X-API-Key": "outage"})

    body = client.get("/metrics").text
    assert _sample(body, "rl_redis_errors_total") - before == 1
    assert _sample(body, "rl_degraded_requests_total", policy="fail_open") >= 1
