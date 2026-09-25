"""Prove the rate limit is really shared across every API instance.

Three containers behind Nginx is not the interesting part - anyone can run
three containers. The claim worth proving is that they enforce ONE limit
between them, and that they keep doing it when requests arrive all at once.

    python scripts/prove_distributed.py
    python scripts/prove_distributed.py --with-failure

Run it against a live stack (`docker compose up -d`).
"""

from __future__ import annotations

import argparse
import collections
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

GATEWAY = "http://localhost:8080"
INSTANCES = ["api1", "api2", "api3"]


def get(url: str, key: str) -> tuple[int, str]:
    """Returns (status, instance that answered). A 429 is an answer, not an error."""
    req = urllib.request.Request(url, headers={"X-API-Key": key})
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return r.status, (r.headers.get("X-Instance") or "?")[:12]
    except urllib.error.HTTPError as e:
        return e.code, (e.headers.get("X-Instance") or "?")[:12]


def health() -> dict:
    return json.loads(urllib.request.urlopen(f"{GATEWAY}/health", timeout=10).read())


def configured_limit() -> int:
    """Read the limit off a response rather than hard-coding it."""
    req = urllib.request.Request(
        f"{GATEWAY}/api/data", headers={"X-API-Key": f"probe-{time.time()}"}
    )
    return int(urllib.request.urlopen(req, timeout=10).headers["X-RateLimit-Limit"])


def prove_shared_limit(limit: int) -> bool:
    """The core proof: talk to each container DIRECTLY, bypassing Nginx.

    Nginx is not in the path here, so nothing we see can be explained away as
    clever load balancing. If the containers each counted alone, every one of
    them would grant a full `limit` and we would see limit x 3 allowed.
    """
    print(f"\n{'=' * 66}\n1. ONE LIMIT SHARED ACROSS THREE CONTAINERS\n{'=' * 66}")
    total_requests = limit + 5
    key = f"shared-{int(time.time() * 1000)}"
    print(f"key={key}  limit={limit}")
    print(f"sending {total_requests} requests straight at the containers, round-robin")

    # Issued from inside the Docker network so api1/api2/api3 are reachable by
    # name and Nginx never sees the traffic.
    probe = f"""
import urllib.request, urllib.error, json
rows = []
for i in range({total_requests}):
    target = {INSTANCES!r}[i % 3]
    req = urllib.request.Request(
        'http://%s:8000/api/data' % target, headers={{'X-API-Key': {key!r}}}
    )
    try:
        r = urllib.request.urlopen(req)
        code, inst = 200, r.headers.get('X-Instance')
    except urllib.error.HTTPError as e:
        code, inst = e.code, e.headers.get('X-Instance')
    rows.append((i + 1, target, code, (inst or '?')[:12]))
print(json.dumps(rows))
"""
    out = subprocess.run(
        ["docker", "compose", "exec", "-T", "api1", "python", "-c", probe],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        sys.exit(f"docker compose exec api1 failed:\n{out.stderr}")
    rows = json.loads(out.stdout.strip().splitlines()[-1])

    print(f"\n{'#':>3}  {'sent to':<8}  {'status':<6}  answered by")
    for number, target, code, instance in rows:
        print(
            f"{number:>3}  {target:<8}  {'OK' if code == 200 else '429':<6}  {instance}"
        )

    allowed = collections.Counter(t for _, t, c, _ in rows if c == 200)
    total = sum(allowed.values())
    answered_by = {inst for _, _, c, inst in rows if c == 200}

    print(f"\nallowed per container  : {dict(allowed)}")
    print(f"TOTAL ALLOWED          : {total}   (configured limit: {limit})")
    print(f"distinct containers    : {len(answered_by)}")
    print(f"if each counted alone  : {limit * 3}")

    ok = total == limit and len(answered_by) > 1
    print(
        f"\n{'PASS' if ok else 'FAIL'}: {total} allowed across {len(answered_by)}"
        f" containers - one shared budget, not one budget each."
    )
    return ok


def _race(concurrency: int, rnd: int) -> tuple[int, int, int, collections.Counter[str]]:
    """Fire `concurrency` requests at one key as simultaneously as possible."""
    key = f"conc-{int(time.time() * 1000)}-{rnd}"
    barrier = threading.Barrier(concurrency)
    winners: collections.Counter[str] = collections.Counter()
    lock = threading.Lock()

    def one(_: int) -> int:
        barrier.wait()  # hold every thread until the last one is ready
        code, instance = get(f"{GATEWAY}/api/data", key)
        if code == 200:
            with lock:
                winners[instance] += 1
        return code

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        codes = collections.Counter(pool.map(one, range(concurrency)))

    errors = sum(v for c, v in codes.items() if c not in (200, 429))
    return codes[200], codes[429], errors, winners


def prove_concurrency(limit: int, concurrency: int, rounds: int) -> bool:
    """No overshoot when every request arrives at the same instant.

    This is what the Lua script buys. Trim, count and add run inside Redis as
    one atomic step, so two instances can never both spend the same last slot.
    """
    print(
        f"\n{'=' * 66}\n2. NO OVERSHOOT UNDER {concurrency}-WAY CONCURRENCY\n{'=' * 66}"
    )
    print(
        f"{concurrency} simultaneous requests on ONE key, through Nginx, limit {limit}\n"
    )

    all_ok = True
    for rnd in range(1, rounds + 1):
        allowed, rejected, errors, winners = _race(concurrency, rnd)
        good = allowed == limit and errors == 0
        all_ok &= good
        print(
            f"  round {rnd}: allowed={allowed:<3} rejected={rejected:<4}"
            f" errors={errors:<3} {'PASS' if good else 'FAIL'}"
            f"   winners: {dict(winners)}"
        )

    print(
        f"\n{'PASS' if all_ok else 'FAIL'}: never more than the limit, however hard"
        f" the requests race."
    )
    return all_ok


def prove_failure(limit: int) -> bool:
    """What happens when the shared state disappears entirely."""
    print(f"\n{'=' * 66}\n3. REDIS OUTAGE\n{'=' * 66}")
    policy = health()["policy"]
    expected = 200 if policy == "fail_open" else 429
    print(f"configured policy: {policy} (expecting {expected} during the outage)\n")

    print("  $ docker compose stop redis")
    subprocess.run(["docker", "compose", "stop", "redis"], capture_output=True)
    time.sleep(2)

    rows = []
    for _ in range(5):
        req = urllib.request.Request(
            f"{GATEWAY}/api/data", headers={"X-API-Key": "outage"}
        )
        try:
            r = urllib.request.urlopen(req, timeout=10)
            rows.append((r.status, r.headers.get("X-RateLimit-Degraded")))
        except urllib.error.HTTPError as e:
            rows.append((e.code, e.headers.get("X-RateLimit-Degraded")))
    for code, degraded in rows:
        print(f"    {code}   X-RateLimit-Degraded: {degraded}")

    during = health()
    print(f"  /health -> status={during['status']} redis={during['redis']}")

    print("\n  $ docker compose start redis")
    subprocess.run(["docker", "compose", "start", "redis"], capture_output=True)
    for _ in range(30):
        try:
            if health()["redis"]:
                break
        except Exception:  # noqa: BLE001 - still coming back up
            pass
        time.sleep(1)

    key = f"recover-{int(time.time() * 1000)}"
    codes = collections.Counter(
        get(f"{GATEWAY}/api/data", key)[0] for _ in range(limit + 2)
    )
    print(f"  after recovery: allowed={codes[200]} rejected={codes[429]} (limit {limit})")

    ok = (
        all(c == expected for c, _ in rows)
        and all(d == "true" for _, d in rows)
        and codes[200] == limit
    )
    print(
        f"\n{'PASS' if ok else 'FAIL'}: outage handled by the {policy} policy, flagged"
        f" as degraded, limits restored afterwards."
    )
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--concurrency", type=int, default=100)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument(
        "--with-failure", action="store_true", help="also stop and start Redis"
    )
    args = parser.parse_args()

    try:
        if not health()["redis"]:
            sys.exit("Redis is down - start it before running the proof.")
        limit = configured_limit()
    except urllib.error.URLError as e:
        sys.exit(f"Cannot reach {GATEWAY} ({e}). Is `docker compose up -d` running?")

    results = {
        "shared limit": prove_shared_limit(limit),
        f"{args.concurrency}-way concurrency": prove_concurrency(
            limit, args.concurrency, args.rounds
        ),
    }
    if args.with_failure:
        results["redis outage"] = prove_failure(limit)

    print(f"\n{'=' * 66}\nSUMMARY\n{'=' * 66}")
    for name, ok in results.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
