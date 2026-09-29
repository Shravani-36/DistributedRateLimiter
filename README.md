# 🚦 Distributed Rate Limiter

[![CI](https://github.com/Shravani-36/DistributedRateLimiter/actions/workflows/ci.yml/badge.svg)](https://github.com/Shravani-36/DistributedRateLimiter/actions/workflows/ci.yml)

API rate limiting that holds **across every server**, not once per server.

> Run your API on 3 instances with an in-memory limiter and "10 per minute"
> quietly becomes 30 per minute — each instance counts alone.
> This keeps the count in **Redis**, so the limit is shared.

**FastAPI · Redis · Lua · Docker · Kubernetes · Prometheus · Grafana · k6**

```
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
  curl -s -o /dev/null -w "%{http_code} " -H "X-API-Key: user1" localhost:8080/api/data
done
# 200 200 200 200 200 200 200 200 200 200 429 429
```

---

## 🔬 Proof it's actually distributed

Three containers running proves nothing. This sends requests **straight at each
container**, bypassing Nginx, so nothing can be explained away as load balancing:

```bash
python scripts/prove_distributed.py --with-failure
```

```
 #  sent to   status  answered by
10  api1      OK      3d60c5812ca5
11  api2      429     3033c8582dd4     <-- api2 rejects its 4th request
12  api3      429     b4cf9ceb9a94

allowed per container : {'api1': 4, 'api2': 3, 'api3': 3}
TOTAL ALLOWED         : 10   (limit: 10)
if each counted alone : 30
```

Request 11 is the whole project in one line: **api2 rejects a request it never
served**, because api1 and api3 already spent the budget.

The same script also fires **100 simultaneous requests** at one key (`allowed=10,
rejected=90, errors=0`, three rounds) and kills Redis to show the failure policy.

---

## 🧠 How it works

**Why Redis?** The limit must be one number every instance can see. In process
memory it isn't shared — 3 instances × 10 becomes 30.

**Why Lua?** Because *check* and *increment* must be one step. Two instances can
otherwise both read "9 used", both see room, and both admit a request — 11 through
a limit of 10. Redis runs the script atomically, so trim → count → add can't be
split. That's why 100-way concurrency gives exactly 10, not "about 10".

**Whose clock?** Redis'. Every script reads `redis.call('TIME')` rather than the
API container's clock, so drift between hosts can't make one instance think a
request has aged out while another still counts it. One clock, one decision.

### Three algorithms

| `ALGORITHM` | Strength | Weakness | Memory per client |
|---|---|---|---|
| `fixed` | Simplest, cheapest | Boundary burst — 2× the limit across an edge | 1 integer |
| `sliding` *(default)* | Accurate rolling limit | Stores every request in the window | N entries |
| `token_bucket` | Allows controlled bursts | Approximates; no exact "N per window" | 2 numbers |

**Fixed window** chops time into buckets and counts per bucket. A client can send
the full limit at `0:59` and again at `1:01` — double the limit in two seconds. A
load test caught exactly that: fixed allowed **10,000** where sliding allowed **5,000**.

**Sliding window** keeps a timestamp per request and always looks at the last
`window` seconds, so that burst is rejected. It frees the whole allowance at once
when old requests age out.

**Token bucket** holds `burst` tokens, refills at `RATE_LIMIT/WINDOW_SECONDS` per
second, and spends one per request. An idle client banks a spike; a busy one is
*paced* — one token drips back at a time instead of the window reopening in a clump.
Memory is two numbers per client no matter the traffic.

```
sliding : ██████████░░░░░░░░░░ ──▶ ██████████   all 10 back at once
bucket  : ██████████░░░░░░░░░░ ──▶ █░█░█░█░█░   one back every 6s
```

---

## 💥 When Redis dies

There's no way to know if a client is over its limit, so the policy is explicit
rather than accidental:

| `FAIL_OPEN` | Behaviour | Use when |
|---|---|---|
| `true` *(default)* | Serve the request | Availability matters most |
| `false` | Reject with `429` | Exceeding the limit is worse than downtime |

Either way it's logged, counted, and flagged with `X-RateLimit-Degraded: true`.
`/health` reports `degraded`; limits resume automatically when Redis returns.

**Liveness ≠ readiness.** Readiness uses `/health`; liveness is a TCP check. A
liveness probe on `/health` would restart every pod in a loop whenever Redis blipped.

---

## 📊 Results

k6, 100 VUs, 3 instances. Throughput varies with hardware; **correctness doesn't**:

| | Linux (4 vCPU) | Windows (Docker Desktop) |
|---|---|---|
| Throughput | 3,908 req/s | 930 req/s |
| p95 latency | 27ms | 185ms |
| Errors | 0.00% | 0.00% |
| Accuracy test — 200 concurrent on one key | exactly 50 of 50 ✅ | exactly 10 of 10 ✅ |

The limiter exports its own decision time, which separates it from the environment:

| | p50 | p95 |
|---|---|---|
| **Limiter decision** | 1.27ms | **2.40ms** |
| End-to-end request | 27.8ms | **185ms** |

The rate limiter is **~1.3% of p95 latency** — the rest is Docker Desktop's WSL2
network path. 📄 [Full results + how to re-run](loadtest/RESULTS.md)

---

## ⚙️ Config & API

| Variable | Default | |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | shared state |
| `RATE_LIMIT` | `10` | requests per window |
| `WINDOW_SECONDS` | `60` | window length |
| `ALGORITHM` | `sliding` | `fixed`, `sliding` or `token_bucket` |
| `BURST` | `0` | token_bucket only; `0` = same as `RATE_LIMIT` |
| `FAIL_OPEN` | `true` | behaviour when Redis is down |

`GET /api/data` is rate limited. `/health`, `/metrics` and `/docs` are exempt, so
a throttled client can't blind your monitoring.

Responses carry `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Algorithm`,
`Retry-After` and `X-Instance` (which instance answered).

> ⚠️ **The API key is used for rate-limit identification in this demonstration;
> it is not an authentication mechanism.** It's read from the `X-API-Key` header
> and never verified. Real deployments would derive identity from a validated
> token *before* the limiter sees it.

---

## 📈 Monitoring · 🧪 Tests · ☸️ Kubernetes

```bash
pytest                                   # 32 tests, no Redis needed (fakeredis)
kubectl apply -f k8s/                    # 3 pods + Redis + HPA 3→10 on CPU
```

Prometheus scrapes all three instances; the Grafana dashboard is provisioned from
files. Metrics: `rl_requests_total{result}` (allowed/rejected), `rl_check_duration_seconds`
(histogram), `rl_redis_errors_total`, `rl_degraded_requests_total`.

CI runs ruff, an integration job against a **real** Redis, a Docker build, and
strict validation of the K8s manifests. 📄 [K8s runbook](k8s/README.md)

> ⚠️ Grafana runs with **anonymous Admin access** so the demo needs no login.
> Local development only — never expose this stack to a network you don't control.

---

## ⚖️ Trade-offs

| Decision | Why | What it costs |
|---|---|---|
| **Redis** for shared state | One counter every instance can see | A network hop, and a component that can fail |
| **Sliding window** | No boundary burst | One sorted-set entry per request, vs one integer |
| **Lua script** | Atomic, so concurrency can't overshoot | Logic lives in Redis — harder to debug |
| **Fail-open** default | A Redis outage shouldn't take the API down | An abusive client is unlimited during an outage |
| **Nginx** in front | Proves the limit holds *across* instances | Another hop; needs DNS re-resolution to avoid stale pods |
| **Header API key** | Keeps the demo focused on limiting | Unauthenticated — fine for a demo, not production |

---

## 🔭 Limits & next steps

A **production-oriented** rate limiter — built with the concerns a real one has
(atomicity, shared state, failure policy, probes, metrics) — but not a fully
production-ready service. The honest gaps:

- **Redis is a single point of failure** — one instance, no replication → *Redis Sentinel*
- **The sliding window's memory grows with traffic** — one entry per request.
  `ALGORITHM=token_bucket` is already O(1) per client if that matters more than
  an exact "N per window"
- **One limit for everyone** → *per-plan limits (`free` 10/min, `pro` 1000/min)*
- **API keys aren't authenticated** · single region

📚 Step-by-step build notes: [ROADMAP.md](ROADMAP.md)
