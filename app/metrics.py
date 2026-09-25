"""Prometheus metrics for the rate limiter.

Scraped from /metrics. Note that each API process keeps its own registry, so
Prometheus must scrape every instance and you sum across them in PromQL --
which is exactly what the dashboard queries do.
"""

from prometheus_client import Counter, Histogram

REQUESTS = Counter(
    "rl_requests_total",
    "Requests seen by the rate limiter",
    ["result", "algorithm"],  # result: allowed | blocked
)

DEGRADED = Counter(
    "rl_degraded_requests_total",
    "Requests decided by the fallback policy because Redis was unreachable",
    ["policy"],  # policy: fail_open | fail_closed
)

REDIS_ERRORS = Counter(
    "rl_redis_errors_total",
    "Redis failures hit while checking a limit",
)

CHECK_DURATION = Histogram(
    "rl_check_duration_seconds",
    "Time spent deciding whether a request is allowed",
    ["algorithm"],
    # A limit check is one or two Redis round trips, so the interesting range
    # is sub-millisecond to a few tens of milliseconds. The default buckets
    # start at 5ms and would put almost everything in one bucket.
    buckets=(0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 1.0),
)
