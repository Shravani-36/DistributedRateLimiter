# 🚦 Distributed Rate Limiter — Step-by-Step Roadmap

> ✅ **All 12 phases are built.** This file is kept as the learning path and
> the explanation of each step; see [README.md](README.md) for how to run the
> finished system.

A beginner-friendly plan to build this project from a **brand-new laptop** to a **deployed, load-tested, monitored** system.

⏱️ Estimated time: **4–6 weeks** (1–2 hrs/day)

---

## 🧭 The Big Picture

```
            ┌──────────────┐
 k6 load ──▶│    Nginx     │  (load balancer)
  test      └──────┬───────┘
         ┌─────────┼─────────┐
         ▼         ▼         ▼
     ┌───────┐ ┌───────┐ ┌───────┐
     │ API 1 │ │ API 2 │ │ API 3 │   FastAPI instances
     └───┬───┘ └───┬───┘ └───┬───┘
         └─────────┼─────────┘
                   ▼
              ┌─────────┐
              │  Redis  │   shared counters (one source of truth)
              └─────────┘

 Prometheus ──scrapes──▶ APIs  ──▶  Grafana dashboards
```

---

## ✅ Phase 0 — Install Everything (Day 1)

| # | Tool | Why | Check it works |
|---|------|-----|----------------|
| 1 | **Git** | version control | `git --version` |
| 2 | **Python 3.12** | backend | `python --version` |
| 3 | **VS Code** + Python extension | editor | — |
| 4 | **Docker Desktop** (includes Compose) | containers | `docker --version`, `docker compose version` |
| 5 | **k6** | load testing | `k6 version` |
| 6 | **kubectl** | talk to Kubernetes | `kubectl version --client` |
| 7 | **minikube** (or Docker Desktop's built-in Kubernetes) | local K8s cluster | `minikube version` |
| 8 | Postman / Thunder Client (optional) | test APIs | — |

💡 The k6 MSI needs an admin prompt. To skip installing it entirely, run k6
as a container instead - see [loadtest/RESULTS.md](loadtest/RESULTS.md).

💡 **Windows users:** install **WSL2** first (`wsl --install`), then Docker Desktop. It makes everything smoother.

❌ You do **NOT** need to install Redis, Prometheus, or Grafana directly — they run inside Docker.

### Install commands

**Windows (PowerShell as admin):**
```powershell
winget install Git.Git Python.Python.3.12 Microsoft.VisualStudioCode Docker.DockerDesktop GrafanaLabs.k6 Kubernetes.kubectl Kubernetes.minikube
```

**macOS (Homebrew):**
```bash
brew install git python@3.12 k6 kubectl minikube
brew install --cask visual-studio-code docker
```

**Ubuntu/Linux:**
```bash
sudo apt update && sudo apt install -y git python3 python3-venv python3-pip
# Docker: https://docs.docker.com/engine/install/ubuntu/
# k6:      https://grafana.com/docs/k6/latest/set-up/install-k6/
# kubectl + minikube: https://minikube.sigs.k8s.io/docs/start/
```

### Git setup
```bash
git config --global user.name  "Your Name"
git config --global user.email "you@example.com"
git clone https://github.com/shravani-36/distributed-rate-limiter.git
cd distributed-rate-limiter
```

---

## ✅ Phase 1 — Project Setup (Day 2)

### Folder structure
```
distributed-rate-limiter/
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI app + routes
│   ├── config.py            # settings (limits, redis url)
│   ├── redis_client.py      # Redis connection
│   ├── limiter/
│   │   ├── __init__.py
│   │   ├── base.py          # common interface
│   │   ├── fixed_window.py
│   │   └── sliding_window.py
│   └── metrics.py           # Prometheus metrics
├── tests/
├── loadtest/
│   └── k6_test.js
├── monitoring/
│   ├── prometheus.yml
│   └── grafana/
├── k8s/
├── nginx/
│   └── nginx.conf
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```

### Virtual environment
```bash
python -m venv venv
# Windows:  venv\Scripts\activate
# Mac/Linux: source venv/bin/activate
```

### `requirements.txt`
```
fastapi
uvicorn[standard]
redis
pydantic-settings
prometheus-client
pytest
httpx
fakeredis
```
```bash
pip install -r requirements.txt
```

Add a `.gitignore` with `venv/`, `__pycache__/`, `.env`.

---

## ✅ Phase 2 — Basic FastAPI App (Day 3)

`app/main.py`
```python
from fastapi import FastAPI

app = FastAPI(title="Distributed Rate Limiter")

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/api/data")
def get_data():
    return {"message": "Here is your data 🎉"}
```

Run it:
```bash
uvicorn app.main:app --reload
```
Open 👉 http://localhost:8000/docs

---

## ✅ Phase 3 — Run Redis + Connect (Day 4)

Start Redis with Docker:
```bash
docker run -d --name redis -p 6379:6379 redis:7
```

`app/config.py`
```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    redis_url: str = "redis://localhost:6379/0"
    rate_limit: int = 10          # requests
    window_seconds: int = 60      # per 60 seconds
    algorithm: str = "sliding"    # "fixed" or "sliding"
    fail_open: bool = True        # allow traffic if Redis is down

settings = Settings()
```

`app/redis_client.py`
```python
import redis
from app.config import settings

r = redis.Redis.from_url(
    settings.redis_url,
    decode_responses=True,
    socket_timeout=0.2,           # don't hang if Redis is slow
    socket_connect_timeout=0.2,
)
```

---

## ✅ Phase 4 — Fixed-Window Algorithm (Days 5–7)

💡 **Idea:** chop time into buckets (e.g. each minute). Count requests per bucket. Over the limit → `429`.

`app/limiter/fixed_window.py`
```python
import time

class FixedWindowLimiter:
    def __init__(self, redis, limit, window):
        self.redis, self.limit, self.window = redis, limit, window

    def allow(self, client_id: str):
        bucket = int(time.time() // self.window)
        key = f"rl:fixed:{client_id}:{bucket}"

        pipe = self.redis.pipeline()          # atomic-ish, one round trip
        pipe.incr(key)
        pipe.expire(key, self.window)
        count, _ = pipe.execute()

        remaining = max(0, self.limit - count)
        retry_after = self.window - int(time.time() % self.window)
        return count <= self.limit, remaining, retry_after
```

### Plug it into FastAPI (per API key)
```python
from fastapi import Request
from fastapi.responses import JSONResponse

@app.middleware("http")
async def rate_limit(request: Request, call_next):
    if request.url.path in ("/health", "/metrics"):
        return await call_next(request)

    client_id = request.headers.get("X-API-Key") or request.client.host
    allowed, remaining, retry_after = limiter.allow(client_id)

    if not allowed:
        return JSONResponse(
            {"detail": "Too Many Requests"},
            status_code=429,
            headers={"Retry-After": str(retry_after),
                     "X-RateLimit-Limit": str(settings.rate_limit),
                     "X-RateLimit-Remaining": "0"},
        )

    response = await call_next(request)
    response.headers["X-RateLimit-Limit"] = str(settings.rate_limit)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    return response
```

🧪 Test: send 11 requests quickly → the 11th should return **429**.
```bash
for i in $(seq 1 12); do curl -s -o /dev/null -w "%{http_code}\n" -H "X-API-Key: user1" localhost:8000/api/data; done
```

### Per-user / per-plan limits (bonus)
Store plans in Redis, e.g. `HSET plan:user1 limit 100`, and read the limit per API key (`free` = 10/min, `pro` = 100/min).

---

## ✅ Phase 5 — Sliding-Window Algorithm (Days 8–11)

### ⚠️ The problem with fixed window
Limit = 10/min. User sends 10 at **0:59** and 10 at **1:01** → **20 requests in 2 seconds** 😱 (boundary burst).

### 💡 The fix: sliding-window log
Keep a **sorted set** of request timestamps. Every request:
1. Delete timestamps older than `now - window`
2. Count what's left
3. If under the limit → add this request

Do it in a **Lua script** so all steps run **atomically** in Redis (no race conditions between API servers).

`app/limiter/sliding_window.py`
```python
import time, uuid

SLIDING_LUA = """
local key    = KEYS[1]
local now    = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit  = tonumber(ARGV[3])
local member = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local count = redis.call('ZCARD', key)

if count < limit then
    redis.call('ZADD', key, now, member)
    redis.call('PEXPIRE', key, window)
    return {1, limit - count - 1}
end
return {0, 0}
"""

class SlidingWindowLimiter:
    def __init__(self, redis, limit, window):
        self.redis, self.limit = redis, limit
        self.window_ms = window * 1000
        self.script = redis.register_script(SLIDING_LUA)

    def allow(self, client_id: str):
        now_ms = int(time.time() * 1000)
        member = f"{now_ms}-{uuid.uuid4().hex}"
        allowed, remaining = self.script(
            keys=[f"rl:sliding:{client_id}"],
            args=[now_ms, self.window_ms, self.limit, member],
        )
        return bool(allowed), remaining, 1
```

🎓 **Learn & write in README:** compare Fixed Window vs Sliding Log vs Sliding Window Counter vs Token Bucket (memory, accuracy, speed).

---

## ✅ Phase 6 — Tests (Days 12–13)

Use `fakeredis` so tests don't need a real Redis.

`tests/test_limiter.py`
```python
import fakeredis
from app.limiter.fixed_window import FixedWindowLimiter

def test_blocks_after_limit():
    r = fakeredis.FakeRedis(decode_responses=True)
    lim = FixedWindowLimiter(r, limit=3, window=60)
    results = [lim.allow("u1")[0] for _ in range(4)]
    assert results == [True, True, True, False]

def test_users_are_independent():
    r = fakeredis.FakeRedis(decode_responses=True)
    lim = FixedWindowLimiter(r, limit=1, window=60)
    assert lim.allow("u1")[0]
    assert lim.allow("u2")[0]
```
```bash
pytest -v
```
Add the same tests for the sliding window (fakeredis supports Lua with `pip install fakeredis[lua]`).

---

## ✅ Phase 7 — Docker + Multiple Instances (Days 14–16)

`Dockerfile`
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

`nginx/nginx.conf`
```nginx
events {}
http {
  upstream api { server api1:8000; server api2:8000; server api3:8000; }
  server {
    listen 80;
    location / {
      proxy_pass http://api;
      proxy_set_header X-API-Key $http_x_api_key;
    }
  }
}
```

`docker-compose.yml`
```yaml
services:
  redis:
    image: redis:7
    ports: ["6379:6379"]

  api1: &api
    build: .
    environment:
      REDIS_URL: redis://redis:6379/0
    depends_on: [redis]
  api2: *api
  api3: *api

  nginx:
    image: nginx:alpine
    volumes: ["./nginx/nginx.conf:/etc/nginx/nginx.conf:ro"]
    ports: ["8080:80"]
    depends_on: [api1, api2, api3]
```
```bash
docker compose up --build
```

🧪 **The key proof:** hit `localhost:8080` 12 times → requests go to **different** servers, but the limit is **still enforced** because they all share Redis. ✅ That's what makes it *distributed*.

💡 Add an `instance` field in your response (`socket.gethostname()`) so you can *see* the load balancing.

---

## ✅ Phase 8 — Redis Failure Handling (Days 17–18)

What if Redis dies? You must choose:

| Strategy | Behaviour | Good for |
|----------|-----------|----------|
| **Fail-open** | allow all requests | user-facing APIs (availability first) |
| **Fail-closed** | block all requests | payments / security-sensitive APIs |
| **Local fallback** | use an in-memory limiter per instance | best of both (approximate) |

```python
import redis, logging

def check(client_id):
    try:
        return limiter.allow(client_id)
    except redis.exceptions.RedisError as e:
        logging.warning("Redis unavailable: %s", e)
        REDIS_ERRORS.inc()                      # Prometheus counter
        if settings.fail_open:
            return True, -1, 0
        return False, 0, 1
```

🧪 Test it:
```bash
docker compose stop redis     # API should keep responding (fail-open)
docker compose start redis    # limits come back automatically
```

Also make `/health` report Redis status (`r.ping()`).

---

## ✅ Phase 9 — Load Testing with k6 (Days 19–21)

`loadtest/k6_test.js`
```javascript
import http from 'k6/http';
import { check } from 'k6';

export const options = {
  stages: [
    { duration: '30s', target: 50 },
    { duration: '1m',  target: 200 },
    { duration: '30s', target: 0 },
  ],
};

export default function () {
  const key = `user${Math.floor(Math.random() * 100)}`;
  const res = http.get('http://localhost:8080/api/data', {
    headers: { 'X-API-Key': key },
  });
  check(res, { 'status is 200 or 429': (r) => r.status === 200 || r.status === 429 });
}
```
```bash
k6 run loadtest/k6_test.js
```

📊 Record in README: **requests/sec, p95 latency, % of 429s**, for 1 vs 3 instances, and fixed vs sliding.

---

## ✅ Phase 10 — Prometheus + Grafana (Days 22–25)

`app/metrics.py`
```python
from prometheus_client import Counter, Histogram

REQUESTS      = Counter("rl_requests_total", "Requests", ["result"])  # allowed / blocked
REDIS_ERRORS  = Counter("rl_redis_errors_total", "Redis failures")
LIMIT_LATENCY = Histogram("rl_check_seconds", "Time to check the limit")
```
Expose them:
```python
from prometheus_client import make_asgi_app
app.mount("/metrics", make_asgi_app())
```

`monitoring/prometheus.yml`
```yaml
global:
  scrape_interval: 5s
scrape_configs:
  - job_name: api
    static_configs:
      - targets: ["api1:8000", "api2:8000", "api3:8000"]
```

Add to `docker-compose.yml`:
```yaml
  prometheus:
    image: prom/prometheus
    volumes: ["./monitoring/prometheus.yml:/etc/prometheus/prometheus.yml:ro"]
    ports: ["9090:9090"]

  grafana:
    image: grafana/grafana
    ports: ["3000:3000"]
```

- Prometheus 👉 http://localhost:9090
- Grafana 👉 http://localhost:3000 (admin / admin) → add data source `http://prometheus:9090`

Useful panels:
- `rate(rl_requests_total{result="allowed"}[1m])`
- `rate(rl_requests_total{result="blocked"}[1m])`
- `histogram_quantile(0.95, rate(rl_check_seconds_bucket[1m]))`
- `rate(rl_redis_errors_total[1m])`

📸 Run k6 while watching Grafana → screenshot for README.

---

## ✅ Phase 11 — Kubernetes (Days 26–30)

```bash
minikube start
eval $(minikube docker-env)          # Windows: minikube docker-env | Invoke-Expression
docker build -t rate-limiter:1.0 .
```

Create in `k8s/`:
- `redis.yaml` → Deployment + Service (`redis`)
- `api.yaml` → Deployment (3 replicas, `REDIS_URL=redis://redis:6379/0`, `imagePullPolicy: Never`, readiness probe on `/health`) + Service (`NodePort`)
- `hpa.yaml` → HorizontalPodAutoscaler (scale 3 → 10 pods on CPU)

```bash
kubectl apply -f k8s/
kubectl get pods
minikube service api --url           # get the URL to test
kubectl scale deployment api --replicas=5
```

🧪 Run k6 against the minikube URL and watch pods scale:
```bash
minikube addons enable metrics-server
kubectl get hpa -w
```

---

## ✅ Phase 12 — Polish & Show Off (Days 31–35)

- 📝 **README**: problem, architecture diagram, algorithms explained, how to run, load-test results, Grafana screenshots, design decisions (fail-open, Lua atomicity)
- 🤖 **GitHub Actions**: run `pytest` on every push
- 🎥 Short demo video / GIF
- 📄 Resume line example:
  > Built a distributed rate limiter (FastAPI + Redis + Lua) enforcing consistent per-API-key limits across 3+ instances; handled ~X req/s at p95 Y ms under k6 load, with Prometheus/Grafana monitoring and Kubernetes autoscaling.

---

## 🗓️ Quick Checklist

- [x] Phase 0 — tools installed
- [x] Phase 1 — project structure + venv
- [x] Phase 2 — FastAPI running
- [x] Phase 3 — Redis connected
- [x] Phase 4 — fixed window + 429 + headers
- [x] Phase 5 — sliding window (Lua)
- [x] Phase 6 — pytest passing
- [x] Phase 7 — Docker Compose, 3 instances + Nginx
- [x] Phase 8 — Redis failure handling
- [x] Phase 9 — k6 load tests + results
- [x] Phase 10 — Prometheus + Grafana dashboards
- [x] Phase 11 — Kubernetes + HPA
- [x] Phase 12 — README, CI, demo

💡 **Commit after every phase** (`git commit -m "phase 4: fixed window limiter"`) so your GitHub history tells the story.
