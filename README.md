# 🚦 Distributed Rate Limiter

[![CI](https://github.com/shravani-36/distributed-rate-limiter/actions/workflows/ci.yml/badge.svg)](https://github.com/shravani-36/distributed-rate-limiter/actions/workflows/ci.yml)

API rate limiting that holds **across every server**, not once per server.

> Run your API on 3 instances with an in-memory limiter and "10 per minute"
> quietly becomes 30 per minute — each instance counts alone.
> This keeps the count in **Redis**, so the limit is shared.

**FastAPI · Redis · Docker · Kubernetes · Prometheus · Grafana · k6**

---

## 🏗️ How it works

```
   client ──▶ Nginx ──┬──▶ API 1 ──┐
                      ├──▶ API 2 ──┼──▶ Redis   ONE counter
                      └──▶ API 3 ──┘            = ONE limit
```

Each request runs an **atomic Lua script** in Redis: drop timestamps older
than the window, count what's left, admit or reject. Because the script runs
inside Redis, two instances can never both spend the same last slot ⚛️

---

## 🐳 Run it

```bash
docker compose up --build
```

| | |
|---|---|
| 🚦 API | http://localhost:8080 |
| 📈 Grafana | http://localhost:3000 |
| 🔍 Prometheus | http://localhost:9090 |

```bash
for i in $(seq 1 12); do
  curl -s -o /dev/null -w "%{http_code} " -H "X-API-Key: user1" localhost:8080/api/data
done
# 200 200 200 200 200 200 200 200 200 200 429 429
```

Requests land on **different instances** (see the `X-Instance` header) and the
limit still holds 🎯

---

## 🔬 Proving it's actually distributed

Three containers running is not the claim. The claim is that they enforce
**one** limit between them. One command proves it:

```bash
python scripts/prove_distributed.py --with-failure
```

### 1. One limit shared across three containers

Nginx is **bypassed** here — requests go straight at each container, so nothing
can be explained away as load-balancer cleverness.

```
  #  sent to   status  answered by
  1  api1      OK      3d60c5812ca5
  2  api2      OK      3033c8582dd4
  3  api3      OK      b4cf9ceb9a94
 ...
 10  api1      OK      3d60c5812ca5
 11  api2      429     3033c8582dd4     <-- api2's first rejection...
 12  api3      429     b4cf9ceb9a94     <-- ...on api2's 4th request

allowed per container  : {'api1': 4, 'api2': 3, 'api3': 3}
TOTAL ALLOWED          : 10   (configured limit: 10)
if each counted alone  : 30
```

Request 11 is the whole project in one line: **api2 rejects a request it never
served**, because api1 and api3 had already spent the budget.

### 2. No overshoot under 100-way concurrency

100 threads released from a barrier at the same instant, on one key:

```
  round 1: allowed=10  rejected=90   errors=0   PASS
  round 2: allowed=10  rejected=90   errors=0   PASS
  round 3: allowed=10  rejected=90   errors=0   PASS
```

**Exactly** the limit, every time — the answer to "how did you handle race
conditions?" is the Lua script below.

### 3. Redis outage

```
  $ docker compose stop redis
    200   X-RateLimit-Degraded: true      # fail_open: still serving
  /health -> status=degraded redis=False
  $ docker compose start redis
  after recovery: allowed=10 rejected=2  # limits back, automatically
```

---

## 📊 Results

k6, 100 virtual users, 3 instances, limit 50/min/key:

| | |
|---|---|
| Throughput | **3,908 req/s** |
| Latency | **p95 27ms** · p99 45ms |
| Failures | **0.00%** |
| Allowed | **exactly 5,000** (100 keys × 50) |

Under 60-way concurrency on a single key it admits **exactly** the limit — no
overshoot.

The same script on a Windows laptop (Docker Desktop/WSL2) gives **930 req/s,
p95 185ms** — 4× slower, and worth showing rather than hiding. The limiter
exports its own decision time, which separates the two:

| | p50 | p95 |
|---|---|---|
| Limiter decision | 1.27ms | **2.40ms** |
| End-to-end request | 27.8ms | **185ms** |

The rate limiter is **~1.3%** of p95 latency; the rest is the environment.
📄 [Full results](loadtest/RESULTS.md)

---

## ⚙️ Config

| Variable | Default | |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | shared state |
| `RATE_LIMIT` | `10` | requests per window |
| `WINDOW_SECONDS` | `60` | window length |
| `ALGORITHM` | `sliding` | or `fixed` |
| `FAIL_OPEN` | `true` | behaviour when Redis is down |

Responses carry `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `Retry-After`
and `X-Instance`.

---

## 🔌 API

| Endpoint | Rate limited | Purpose |
|---|---|---|
| `GET /api/data` | ✅ yes | The demo endpoint the limiter guards |
| `GET /health` | ❌ no | Readiness + Redis status; `503` only when failing closed with Redis down |
| `GET /metrics` | ❌ no | Prometheus scrape target |
| `GET /docs` | ❌ no | Swagger UI |

`/health` and `/metrics` are exempt so a limited client can't blind your
monitoring, and so Prometheus scrapes don't spend anyone's budget.

### Response headers

| Header | Example | Meaning |
|---|---|---|
| `X-RateLimit-Limit` | `10` | Requests allowed per window |
| `X-RateLimit-Remaining` | `7` | Left in this window (`-1` = unknown, Redis down) |
| `X-RateLimit-Algorithm` | `sliding` | Which algorithm decided |
| `X-Instance` | `api2` | Which instance answered |
| `Retry-After` | `53` | Seconds until a slot frees (on `429`) |
| `X-RateLimit-Degraded` | `true` | Set only when Redis was unreachable |

```bash
curl -i -H "X-API-Key: user1" localhost:8080/api/data
```

> ⚠️ **The API key is used for rate-limit identification in this
> demonstration; it is not an authentication mechanism.** It is read straight
> from the `X-API-Key` header and never verified, so anyone can present any
> key. Requests without one fall back to the client IP. A real deployment
> would derive the identity from an authenticated session or a validated
> token *before* the limiter sees it.

---

## 🧠 Design decisions

**Why Redis?** The limit has to be one number that every instance can see. In
process memory it isn't shared — 3 instances × 10 becomes 30. Redis is a
single place all instances read and write, it's fast enough to sit in the
request path (sub-millisecond), and keys expire on their own so old windows
clean themselves up.

**Why Lua?** Because `check` and `increment` must be one step. Two instances
can otherwise both read "9 used", both conclude there's room, and both admit a
request — 11 through a limit of 10. Redis runs a Lua script atomically: no
other command interleaves, so the trim → count → add sequence can't be split.
That's what makes the 100-way concurrency result come out at exactly 10 rather
than "about 10". The alternative, `WATCH`/`MULTI` with retries, costs more
round trips and still has to handle contention.

**Sliding window over fixed window.** A fixed window lets a client send the
full limit at `0:59` and again at `1:01` — double the limit in two seconds.
A load test caught exactly that: fixed allowed **10,000** where sliding
allowed **5,000**.

**Redis down → you choose.** During an outage there's no way to know if a
client is over its limit, so the policy is explicit: `FAIL_OPEN=true` keeps
serving (availability), `false` returns 429 (correctness). Either way it's
logged, counted, and flagged with `X-RateLimit-Degraded`.

**Liveness ≠ readiness.** Readiness uses `/health`; liveness is a TCP check.
A liveness probe on `/health` would restart every pod in a loop whenever
Redis blipped.

---

## 🧪 Tests

```bash
pytest        # 32 tests, no Redis needed (fakeredis)
```

CI also runs ruff, an integration job against a **real** Redis, a Docker
build, and strict validation of the Kubernetes manifests.

```bash
python scripts/prove_distributed.py --with-failure   # against a running stack
```

---

## 📈 Monitoring

Prometheus scrapes all three instances every 5s; the Grafana dashboard is
provisioned from files, so it's there the first time you open it.

| | |
|---|---|
| 🔍 Prometheus | http://localhost:9090 |
| 📈 Grafana | http://localhost:3000 → *Distributed Rate Limiter* |

| Metric | Type | What it answers |
|---|---|---|
| `rl_requests_total{result,algorithm}` | counter | Total, allowed and rejected |
| `rl_check_duration_seconds` | histogram | How long a limit decision takes |
| `rl_redis_errors_total` | counter | Redis failures hit while checking |
| `rl_degraded_requests_total{policy}` | counter | Decisions made by the fallback policy |

Allowed and rejected are one metric split by a `result` label rather than two
separate counters, so a ratio is a single PromQL expression:

```promql
sum(rate(rl_requests_total{result="blocked"}[1m])) / sum(rate(rl_requests_total[1m]))
```

Each instance keeps its own counters, so the dashboard sums across them.

> ⚠️ **Local development only.** Grafana runs with anonymous Admin access
> (`GF_AUTH_ANONYMOUS_ENABLED=true`) and a default `admin` password so the
> demo needs no login. **Never expose this stack to a network you don't
> control.** Anything reachable at `:3000` gets full Grafana admin.

---

## 🏋️ Load testing

```bash
k6 run loadtest/load_test.js        # throughput and latency
RATE_LIMIT=10 k6 run loadtest/accuracy_test.js   # correctness under contention
```

No k6 installed? Run it as a container — on Windows this avoids the MSI's
admin prompt:

```bash
docker run --rm -i --network distributed-rate-limiter_default \
  -e BASE_URL=http://nginx:80 -e RATE_LIMIT=10 \
  grafana/k6 run - < loadtest/accuracy_test.js
```

📄 Measured numbers from both environments: [loadtest/RESULTS.md](loadtest/RESULTS.md)

---

## ☸️ Kubernetes

```bash
eval $(minikube docker-env) && docker build -t rate-limiter:1.0 .
kubectl apply -f k8s/
```

3 pods + Redis + autoscaling 3 → 10 on CPU. 📄 [Runbook](k8s/README.md)

💡 More pods raise how much traffic the API can **handle** — not anyone's
rate limit. Capacity scales, permission doesn't.

---

## 📁 Layout

```
app/main.py      FastAPI + rate limit middleware
app/limiter/     fixed_window · sliding_window (Lua) · resilient
tests/           32 tests
scripts/         prove_distributed.py — the shared-limit proof
loadtest/        k6 scripts + measured results
monitoring/      Prometheus config + provisioned Grafana dashboard
k8s/             Deployments, Service, ConfigMap, HPA
nginx/           load balancer config (DNS re-resolution)
```

---

## ⚖️ Trade-offs

| Decision | Why | What it costs |
|---|---|---|
| **Redis** for shared state | One counter every instance can see | A network hop per request, and a component that can fail |
| **Sliding window** by default | No boundary burst (2× the limit in 2s) | One sorted-set entry per request vs a single integer |
| **Lua script** | Trim/count/add is atomic, so concurrency can't overshoot | Logic lives in Redis, not Python — harder to debug |
| **Fail-open** by default | A Redis outage shouldn't take the API down with it | An abusive client is unlimited during an outage |
| **Nginx** in front | Shows the limit holding across instances, not just within one | Another hop; needed DNS re-resolution to avoid stale instances |
| **Docker Compose** | Whole stack reproducible with one command | Not how you'd run it in production |
| **Kubernetes** manifests | Demonstrates horizontal scaling and probes | More pods raise capacity, not anyone's rate limit |
| **API key from a header** | Keeps the demo focused on limiting | Unauthenticated — trivially spoofed, fine for a demo, not for production |

---

## ⚖️ Known limits

This is a **production-oriented** distributed rate limiter — it is built with
the concerns a real one has (atomicity, shared state, failure policy, probes,
metrics), and it is **not** a fully production-ready service. The honest gaps:

- **Redis is a single point of failure.** One instance, no replication. Real
  HA needs Sentinel or Cluster.
- **API keys aren't authenticated** — read from a header, never verified.
- **Clock skew.** Window edges use each instance's own clock, not Redis's.
  Containers on one host share a clock; pods across nodes may not.
- **Memory grows with traffic.** The sliding window keeps one sorted-set entry
  per request in the window — accurate, but not free.
- **Single region.** Cross-region would need either a shared Redis (latency)
  or per-region limits (weaker guarantee).
- **Grafana is wide open** by design for the local demo (see Monitoring).

---

## 🔭 Future improvements

| | Why |
|---|---|
| **Token bucket** | Allow controlled bursts instead of a hard cliff |
| **Per-plan limits** | `free` 10/min vs `pro` 1000/min, read from Redis per key |
| **Redis Sentinel** | Remove the single point of failure |
| **Sliding-window counter** | Approximate the window with 2 integers instead of N entries — O(1) memory |
| **Real authentication** | Derive identity from a validated token before the limiter sees it |

📚 Step-by-step build notes: [ROADMAP.md](ROADMAP.md)
