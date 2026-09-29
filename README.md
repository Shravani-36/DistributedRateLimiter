# 🚦 Distributed Rate Limiter

[![CI](https://github.com/Shravani-36/DistributedRateLimiter/actions/workflows/ci.yml/badge.svg)](https://github.com/Shravani-36/DistributedRateLimiter/actions/workflows/ci.yml)

API rate limiting that holds **across every server**, not once per server.

> Run your API on 3 instances with an in-memory limiter and "10 per minute"
> quietly becomes 30 per minute — each instance counts alone.
> This keeps the count in **Redis**, so the limit is shared.

**FastAPI · Redis · Lua · Docker · Kubernetes · Prometheus · Grafana · k6**

```text
   client ──▶ Nginx ──┬──▶ API 1 ──┐
                      ├──▶ API 2 ──┼──▶ Redis   ONE counter
                      └──▶ API 3 ──┘            = ONE limit
```

---

## 🐳 Run it

```bash
docker compose up --build
```

🚦 API http://localhost:8080 · 📈 Grafana http://localhost:3000 · 🔍 Prometheus http://localhost:9090

```bash
for i in $(seq 1 12); do
  curl -s -o /dev/null -w "%{http_code} " \
    -H "X-API-Key: user1" \
    localhost:8080/api/data
done

# 200 200 200 200 200 200 200 200 200 200 429 429
```

API documentation:

```text
http://localhost:8080/docs
```

---

## 🔬 Proof it's actually distributed

Three containers running proves nothing. This sends requests **straight at each
container**, bypassing Nginx, so the result cannot be explained by load balancing:

```bash
python scripts/prove_distributed.py --with-failure
```

Example:

```text
 #  sent to   status  answered by
10  api1      OK      3d60c5812ca5
11  api2      429     3033c8582dd4     <-- api2 rejects its 4th request
12  api3      429     b4cf9ceb9a94

allowed per container : {'api1': 4, 'api2': 3, 'api3': 3}
TOTAL ALLOWED         : 10   (limit: 10)
if each counted alone : 30
```

Request 11 demonstrates the distributed behavior: **api2 rejects a request it
never counted locally**, because api1 and api3 already spent the shared budget.

The same script can also send concurrent requests and test Redis failure behavior.

---

## 🧠 How it works

### Why Redis?

The limit must be one shared value that every API instance can see.

With an in-memory limiter:

```text
3 instances × 10 requests = 30 requests
```

With Redis:

```text
API 1 ─┐
API 2 ─┼──▶ Redis ──▶ ONE shared limit
API 3 ─┘
```

### Why Lua?

Rate-limit **check + update** must happen atomically.

Without atomicity, two instances could both read:

```text
9 requests used
```

Both could accept a request and produce:

```text
11 requests
```

for a limit of 10.

The Redis Lua script performs the required operations atomically.

### Shared clock

The rate-limiting scripts use Redis server time rather than each API
container's local clock. This avoids inconsistent window calculations caused
by clock differences between API instances.

---

## 🧮 Rate-Limiting Algorithms

The project supports three algorithms:

| Algorithm      | Strength                            | Trade-off                       | Memory per client |
| -------------- | ----------------------------------- | ------------------------------- | ----------------- |
| `fixed`        | Simple and cheap                    | Boundary burst                  | O(1)              |
| `sliding`      | Accurate rolling window             | Stores requests in the window   | O(N)              |
| `token_bucket` | Controlled bursts and smooth refill | Not an exact N-per-window model | O(1)              |

### Fixed Window

Divides time into fixed intervals.

For example, with a limit of 10/minute:

```text
00:59 → 10 requests
01:00 → counter resets
01:01 → 10 requests
```

A client can therefore make up to 20 requests around a window boundary.

### Sliding Window

Tracks request timestamps and considers only requests inside
the current rolling window.

This avoids the boundary burst of a fixed window.

```text
Old requests
     ↓
[expired] [active requests] ─────────▶ now
             ↑
         count here
```

Redis sorted sets are used to maintain the request timestamps.

### Token Bucket

The token bucket maintains a fixed number of tokens and refills
them continuously.

```text
tokens = burst capacity

request → consume 1 token

refill rate =
RATE_LIMIT / WINDOW_SECONDS
```

An idle client can accumulate tokens and make a controlled burst,
while sustained traffic is limited by the refill rate.

Unlike the sliding window, token bucket uses **O(1) state per client**.

---

## 💥 When Redis dies

There is no reliable way to enforce the shared limit when Redis is unavailable,
so the failure policy is explicit.

| `FAIL_OPEN`        | Behaviour         | Use case                              |
| ------------------ | ----------------- | ------------------------------------- |
| `true` *(default)* | Serve the request | Availability is prioritized           |
| `false`            | Reject with `429` | Rate-limit enforcement is prioritized |

When degraded:

* Requests are logged.
* Metrics record the degraded state.
* `X-RateLimit-Degraded: true` is returned.
* `/health` reports the degraded condition.
* Normal rate limiting resumes when Redis recovers.

**Liveness ≠ readiness.**

Readiness uses `/health`.

The Kubernetes liveness probe uses a TCP check so a temporary Redis
failure does not cause unnecessary container restart loops.

---

## 📊 Results

Load testing was performed with 3 API instances.

Throughput depends on the machine and environment, while correctness
is expected to remain consistent.

| Metric      | Linux (4 vCPU) | Windows (Docker Desktop) |
| ----------- | -------------: | -----------------------: |
| Throughput  |    3,908 req/s |                930 req/s |
| p95 latency |          27 ms |                   185 ms |
| Errors      |          0.00% |                    0.00% |

Limiter decision latency:

| Metric             |     p50 |     p95 |
| ------------------ | ------: | ------: |
| Limiter decision   | 1.27 ms | 2.40 ms |
| End-to-end request | 27.8 ms |  185 ms |

The limiter exports its own decision latency so it can be separated
from infrastructure and network overhead.

📄 [Full load-test results](loadtest/RESULTS.md)

---

## ⚙️ Configuration & API

| Variable         | Default                    | Description                                        |
| ---------------- | -------------------------- | -------------------------------------------------- |
| `REDIS_URL`      | `redis://localhost:6379/0` | Redis connection                                   |
| `RATE_LIMIT`     | `10`                       | Requests per window                                |
| `WINDOW_SECONDS` | `60`                       | Window length                                      |
| `ALGORITHM`      | `sliding`                  | `fixed`, `sliding`, or `token_bucket`              |
| `BURST`          | `0`                        | Token-bucket burst capacity; `0` uses `RATE_LIMIT` |
| `FAIL_OPEN`      | `true`                     | Behavior when Redis is unavailable                 |

### Endpoints

| Endpoint        | Purpose                       |
| --------------- | ----------------------------- |
| `GET /api/data` | Rate-limited API endpoint     |
| `GET /health`   | Health/readiness status       |
| `GET /metrics`  | Prometheus metrics            |
| `GET /docs`     | Swagger/OpenAPI documentation |

The health, metrics, and documentation endpoints are exempt from
rate limiting so monitoring remains available even when a client is throttled.

Responses include:

```text
X-RateLimit-Limit
X-RateLimit-Remaining
X-RateLimit-Algorithm
Retry-After
X-Instance
```

> ⚠️ **The API key is used only for rate-limit identification in this
> demonstration. It is not an authentication mechanism.**
>
> The `X-API-Key` header is read but never verified. A production system
> should derive the rate-limit identity from an already authenticated
> and validated identity.

---

## 📈 Monitoring

The project includes:

* Prometheus
* Grafana
* Application metrics
* Per-instance metrics
* Rate-limit decision latency
* Redis error tracking
* Degraded request tracking

Important metrics include:

```text
rl_requests_total{result}
rl_check_duration_seconds
rl_redis_errors_total
rl_degraded_requests_total
```

Grafana is provisioned automatically from the repository configuration.

> ⚠️ Grafana uses anonymous Admin access for local development.
> Do not expose this configuration to an untrusted network.

---

## 🧪 Testing

The project includes unit, API, algorithm, and metrics tests.

Run:

```bash
python -m pytest -v
```

Current test suite:

```text
43 passed
```

The tests cover:

* API behavior
* Fixed Window
* Sliding Window
* Token Bucket
* Redis failure behavior
* Metrics
* Rate-limit edge cases

Fakeredis is used where Redis is not required for the test.

---

## ☸️ Kubernetes

Kubernetes manifests are included for:

* API deployment
* Redis
* Readiness/liveness probes
* Horizontal Pod Autoscaler
* Resource requests
* Distributed API replicas

Example:

```bash
kubectl apply -f k8s/
```

The manifests are designed for a Kubernetes environment and are
validated in CI.

📄 [Kubernetes runbook](k8s/README.md)

---

## 🔄 CI/CD

GitHub Actions runs automated checks including:

* Ruff linting
* Formatting checks
* Python tests
* Redis integration testing
* Docker image build
* Kubernetes manifest validation

CI configuration:

```text
.github/workflows/ci.yml
```

---

## ⚖️ Trade-offs

| Decision           | Why                                      | Cost                                    |
| ------------------ | ---------------------------------------- | --------------------------------------- |
| **Redis**          | Shared state across instances            | Network dependency                      |
| **Sliding Window** | Accurate rolling limit                   | O(N) request state                      |
| **Token Bucket**   | Controlled bursts with O(1) state        | Different semantics from exact N/window |
| **Lua**            | Atomic Redis operations                  | Logic is harder to debug                |
| **Fail-open**      | Preserves availability                   | Unlimited requests during Redis outage  |
| **Nginx**          | Distributes traffic across API instances | Additional network hop                  |
| **API key header** | Simple rate-limit identity               | Not authentication                      |

---

## 🔭 Production Considerations

This is a **production-oriented portfolio project**, not a claim of being
a complete production service.

Current implementation demonstrates:

* Distributed shared state
* Atomic rate-limit decisions
* Multiple rate-limit algorithms
* Redis failure handling
* Observability
* Containerization
* Load testing
* Kubernetes deployment manifests
* Automated testing and CI

Potential next steps include:

* Redis replication / Sentinel or managed Redis
* Multi-region deployment
* Authenticated identity-based rate limiting
* Per-plan limits
* Distributed configuration management
* Advanced burst policies
* Security hardening
* Production-grade secret management
* More extensive benchmarking

---

## 📁 Project Structure

```text
DistributedRateLimiter/
├── app/
│   ├── limiter/
│   │   ├── fixed_window.py
│   │   ├── sliding_window.py
│   │   ├── token_bucket.py
│   │   └── resilient.py
│   ├── config.py
│   ├── main.py
│   ├── metrics.py
│   └── redis_client.py
│
├── tests/
│   ├── test_api.py
│   ├── test_fixed_window.py
│   ├── test_sliding_window.py
│   ├── test_token_bucket.py
│   ├── test_resilient.py
│   └── test_metrics.py
│
├── k8s/
├── loadtest/
├── monitoring/
├── nginx/
├── scripts/
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
└── requirements.txt
```

---

## 🎯 Key Learning

This project focuses on the distributed-systems problem behind rate limiting:

> **When multiple API servers handle the same client, how do they enforce one
> consistent limit without relying on local memory?**

The solution combines:

```text
FastAPI
   ↓
Nginx
   ↓
Multiple API instances
   ↓
Redis shared state
   ↓
Atomic Lua scripts
   ↓
Prometheus + Grafana
```

It demonstrates distributed state management, concurrency control,
failure handling, observability, testing, and container orchestration
in one system.
