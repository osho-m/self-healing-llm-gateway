// k6 load profile: mixed realtime/batch traffic across teams and tiers.
//   k6 run loadtests/gateway.js            (BASE_URL defaults to http://localhost:8000)
import http from "k6/http";
import { check } from "k6";
import { Counter } from "k6/metrics";

const BASE = __ENV.BASE_URL || "http://localhost:8000";
const limited = new Counter("gateway_429");
const fallbacks = new Counter("gateway_fallback_responses");

export const options = {
  scenarios: {
    realtime: {
      executor: "constant-arrival-rate", rate: 400, timeUnit: "1s", duration: "60s",
      preAllocatedVUs: 200, maxVUs: 1000, exec: "realtime",
    },
    batch: {
      executor: "constant-arrival-rate", rate: 100, timeUnit: "1s", duration: "60s",
      preAllocatedVUs: 50, maxVUs: 300, exec: "batch",
    },
    noisy_team: {
      executor: "constant-arrival-rate", rate: 20, timeUnit: "1s", duration: "60s",
      preAllocatedVUs: 10, maxVUs: 50, exec: "noisy",
    },
  },
  thresholds: {
    "http_req_failed{expected_response:true}": ["rate<0.01"],
    "http_req_duration{scenario:realtime}": ["p(99)<1000"],
  },
};

http.setResponseCallback(http.expectedStatuses(200, 429));

function send(key, tier, priority) {
  const body = JSON.stringify({
    model: tier, priority, max_tokens: 64,
    messages: [{ role: "user", content: `k6 ${priority} request ${__VU}-${__ITER}` }],
  });
  const res = http.post(`${BASE}/v1/chat/completions`, body, {
    headers: { "Content-Type": "application/json", "X-API-Key": key },
  });
  check(res, { "200 or 429": (r) => r.status === 200 || r.status === 429 });
  if (res.status === 429) limited.add(1);
  if (res.status === 200 && res.json("x_gateway.fallback_used")) fallbacks.add(1);
}

const tiers = ["balanced", "balanced", "premium", "economy"];
export function realtime() { send("loadtest-key", tiers[__ITER % tiers.length], "realtime"); }
export function batch() { send("batch-live-key", "economy", "batch"); }
export function noisy() { send("search-live-key", "economy", "realtime"); }
