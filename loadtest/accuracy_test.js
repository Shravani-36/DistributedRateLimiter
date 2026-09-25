// Does the limit actually hold when everyone arrives at once?
//
//   RATE_LIMIT=50 k6 run loadtest/accuracy_test.js
//
// Load testing a rate limiter is not only about speed. The interesting
// question is correctness under contention: many concurrent virtual users,
// spread over several API instances, all spending ONE shared budget.
//
// Every VU hammers the SAME API key. Whatever the concurrency, exactly
// RATE_LIMIT requests must be allowed - no more (overshoot means a race in
// the limiter), no fewer (undershoot means it is rejecting valid traffic).
//
// Run it against a single instance and against the 3-instance cluster on
// :8080. The numbers should be identical. That is the whole project in one
// test.

import http from 'k6/http';
import { Counter } from 'k6/metrics';

// A 429 is this API working correctly, not a failure. Without this, k6's
// http_req_failed metric would count every throttled request as an error.
http.setResponseCallback(http.expectedStatuses(200, 429));

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8080';
const RATE_LIMIT = parseInt(__ENV.RATE_LIMIT || '50', 10);
const VUS = parseInt(__ENV.VUS || '50', 10);
const REQUESTS_PER_VU = parseInt(__ENV.REQUESTS_PER_VU || '4', 10);

const allowed = new Counter('rl_allowed');
const blocked = new Counter('rl_blocked');

export const options = {
  scenarios: {
    thundering_herd: {
      executor: 'per-vu-iterations',
      vus: VUS,
      iterations: REQUESTS_PER_VU,
      maxDuration: '1m',
    },
  },
  thresholds: {
    // Exactly the limit: no overshoot, no undershoot.
    rl_allowed: [`count<=${RATE_LIMIT}`, `count>=${RATE_LIMIT}`],
    http_req_failed: ['rate==0'],
  },
};

// setup() runs ONCE and hands its result to every VU. Generating the key at
// module level would not work: k6 gives each VU its own JS runtime and runs
// module code again for each one, so every VU would get a different key - and
// therefore its own budget, which is exactly what this test must not do.
export function setup() {
  // A fresh key per run, so a repeat run is not throttled by the previous one.
  return { apiKey: `accuracy-${Date.now()}` };
}

export default function (data) {
  const res = http.get(`${BASE_URL}/api/data`, {
    headers: { 'X-API-Key': data.apiKey },
  });

  if (res.status === 429) {
    blocked.add(1);
  } else if (res.status === 200) {
    allowed.add(1);
  }
}

export function handleSummary(data) {
  const got = data.metrics.rl_allowed ? data.metrics.rl_allowed.values.count : 0;
  const rejected = data.metrics.rl_blocked ? data.metrics.rl_blocked.values.count : 0;
  const verdict = got === RATE_LIMIT ? 'PASS' : got > RATE_LIMIT ? 'FAIL (overshoot)' : 'FAIL (undershoot)';

  return {
    stdout: [
      '',
      `  limit          ${RATE_LIMIT}`,
      `  sent           ${VUS * REQUESTS_PER_VU} (${VUS} concurrent VUs)`,
      `  allowed        ${got}`,
      `  blocked (429)  ${rejected}`,
      `  verdict        ${verdict}`,
      '',
    ].join('\n'),
  };
}
