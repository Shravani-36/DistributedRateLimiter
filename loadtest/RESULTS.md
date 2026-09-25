# 📊 Load test results

Numbers from an actual run, not estimates. Re-run them yourself with the
commands below — your hardware will give different figures.

## 🧪 Setup

| | |
|---|---|
| Machine | 4 vCPU, 15 GB RAM, Linux |
| API | 3 API containers behind Nginx, one uvicorn process each, one shared Redis 7.0 |
| Load generator | k6 v1.1.0, **on the same machine** as the API |
| Limit | 50 requests / 60s per API key |

⚠️ k6 competing with the API for the same 4 CPUs means these latencies are
**pessimistic** and the throughput is a floor, not a ceiling. Run k6 from a
separate machine for clean numbers.

---

## 1️⃣ Throughput — `load_test.js`

100 virtual users, 100 different API keys, ramped over 45s.

```
http_reqs .......... 175,873  (3,908 req/s)
http_req_failed .... 0.00%    (0 out of 175,873)
checks ............. 100.00%  (527,619 passed)

http_req_duration    avg=12.51ms  med=10.98ms
                     p(90)=23.07ms  p(95)=27.41ms  max=102ms

rl_allowed ......... 5,000
rl_blocked ......... 170,873  (97.15%)
```

✅ **Zero failures, zero 5xx.** Every request got a real answer.
✅ **p95 = 27ms** while rejecting ~3,800 requests/second.
🎯 **Exactly 5,000 allowed** = 100 keys × 50 — the limit held perfectly across
all three API containers under sustained load.

---

## 2️⃣ Correctness under contention — `accuracy_test.js`

50 concurrent virtual users, all hammering **the same** API key at once:

```
limit          50
sent           200 (50 concurrent VUs)
allowed        50
blocked (429)  150
verdict        PASS
```

🎯 Exactly the limit — no overshoot (which would mean a race between
instances) and no undershoot (rejecting valid traffic).

---

## 3️⃣ Fixed vs sliding, under load

Same 28s test against each algorithm:

| | 🪣 Fixed | 🎚️ Sliding |
|---|---|---|
| Allowed (limit implies 5,000) | **10,000** ❌ | **5,000** ✅ |
| p95 latency | 29.4ms | 29.4ms |
| avg latency | 13.6ms | 14.9ms |
| Redis memory | 1.31 MB | 1.60 MB |

💥 **The boundary burst, caught in the wild.** The run crossed a one-minute
bucket boundary, so every key got its 50 requests *twice* — exactly double the
configured limit. The sliding window allowed exactly 5,000.

The cost of that correctness: **~9% more latency** and **~22% more Redis
memory** (one sorted-set entry per request in the window, versus one integer
per key). Cheap, for a limit that actually means what it says.

---

## 4️⃣ The same test on Windows / Docker Desktop

The numbers above came from Linux. Re-running the identical script on a
Windows 11 laptop through Docker Desktop (WSL2), against the committed
compose config (**limit 10/60s**, not 50), 100 VUs / 100 keys, 1m45s:

```
http_reqs .......... 97,613   (929.7 req/s)
http_req_failed .... 0.00%    (0 out of 97,613)
checks ............. 100.00%  (292,839 passed)

http_req_duration    med=27.84ms  p(90)=130.11ms
                     p(95)=185.09ms  p(99)=233.22ms  max=392.28ms

rl_allowed ......... 2,000
rl_blocked ......... 95,613   (97.95%)
```

⚠️ **A threshold failed:** `p(95)<150ms` was crossed at **185.09ms**. Recorded
rather than quietly relaxed — that is the point of having a threshold.

### Where that latency actually comes from

The limiter exports its own decision time, so the two can be separated:

| | p50 | p95 | p99 |
|---|---|---|---|
| **Limiter decision** (`rl_check_duration_seconds`) | 1.27ms | **2.40ms** | 2.77ms |
| **End-to-end** (`http_req_duration`) | 27.84ms | **185.09ms** | 233.22ms |

🔍 The limiter is **~1.3%** of p95 latency. The other ~183ms is Docker
Desktop's WSL2 network path, Nginx and uvicorn competing for a laptop's cores
with the load generator running on the same machine. 4× lower throughput than
the Linux box comes from the same place — not from the rate limiter.

✅ `rl_allowed = 2,000` is correct, not a limit breach: the test runs 105s
against a 60-second sliding window, so each of the 100 keys legitimately earns
roughly two windows' worth of budget as its early requests age out.

### Correctness under contention, same machine

`accuracy_test.js` against the 3-container cluster through Nginx, limit 10:

```
  limit          10
  sent           200 (50 concurrent VUs)
  allowed        10
  blocked (429)  190
  verdict        PASS
```

🎯 Throughput varies with hardware; **correctness does not**. The slower
Windows environment admits exactly the same 10.

---

## ▶️ Reproduce

```bash
docker compose up --build -d

# throughput
BASE_URL=http://localhost:8080 k6 run loadtest/load_test.js

# correctness under contention (match RATE_LIMIT to the server's setting)
BASE_URL=http://localhost:8080 RATE_LIMIT=10 k6 run loadtest/accuracy_test.js
```

💡 If your network blocks `stats.grafana.org`, set `K6_NO_USAGE_REPORT=true`.

### No k6 installed?

Run it as a container instead — nothing to install, and it reaches the API
over the compose network:

```bash
docker run --rm -i --network distributed-rate-limiter_default \
  -e BASE_URL=http://nginx:80 -e USERS=100 -e KEYS=100 \
  grafana/k6 run - < loadtest/load_test.js
```

💡 On Windows the k6 MSI needs an admin prompt; the container above does not.
