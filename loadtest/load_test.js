// Throughput and latency under a realistic mixed load.
//
//   k6 run loadtest/load_test.js
//   BASE_URL=http://localhost:8080 USERS=200 k6 run loadtest/load_test.js
//
// Many different API keys hammer the API at once. Some 429s are expected and
// correct - they are the limiter doing its job, not errors. What matters is
// that the limiter stays fast and never 5xxs.

import http from 'k6/http';
import { check } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';

// A 429 is this API working correctly, not a failure. Without this, k6's
// http_req_failed metric would count every throttled request as an error.
http.setResponseCallback(http.expectedStatuses(200, 429));

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8080';
const USERS = parseInt(__ENV.USERS || '200', 10);
const KEYS = parseInt(__ENV.KEYS || '100', 10);

const allowed = new Counter('rl_allowed');
const blocked = new Counter('rl_blocked');
const blockedRate = new Rate('rl_blocked_rate');
const limiterLatency = new Trend('rl_latency', true);

export const options = {
  stages: [
    { duration: '30s', target: USERS / 4 }, // warm up
    { duration: '1m', target: USERS }, // hold at full load
    { duration: '15s', target: 0 }, // ramp down
  ],
  thresholds: {
    // The limiter must never break the API, only throttle it.
    http_req_failed: ['rate==0'],
    // Checking a limit is two Redis round trips at most - keep it cheap.
    'http_req_duration{expected_response:true}': ['p(95)<150', 'p(99)<400'],
  },
};

export default function () {
  const key = `user${Math.floor(Math.random() * KEYS)}`;

  const res = http.get(`${BASE_URL}/api/data`, {
    headers: { 'X-API-Key': key },
    tags: { name: 'api_data' },
  });

  limiterLatency.add(res.timings.duration);

  // 429 is a correct answer, so it counts as a pass. A 5xx never does.
  check(res, {
    'answered 200 or 429': (r) => r.status === 200 || r.status === 429,
    'carries rate limit headers': (r) => r.headers['X-Ratelimit-Limit'] !== undefined,
    'never a server error': (r) => r.status < 500,
  });

  const wasBlocked = res.status === 429;
  blockedRate.add(wasBlocked);
  if (wasBlocked) {
    blocked.add(1);
  } else {
    allowed.add(1);
  }
}
