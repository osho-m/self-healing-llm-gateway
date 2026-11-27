# Benchmarks

How the gateway was load-tested and what was measured. Summary numbers are in the [README](../README.md#results).

## How to run

```bash
python loadtests/loadtest.py --requests 5000 --concurrency 500 [--outage]   # capacity: closed loop, mixed teams/tiers/priorities
python loadtests/loadtest.py --requests 3000 --rate 100                     # overhead SLO: fixed arrival rate (open loop)
k6 run loadtests/gateway.js                                                   # sustained arrival-rate profile
```

`loadtest.py` reads two of the gateway's own histograms:

- `gateway_overhead_seconds`: time in the gateway, excluding provider calls and retry backoff.
- `gateway_store_duration_seconds`: request-path Redis calls. This is a subset of overhead.

Neither figure includes provider latency.

The two modes measure different things:

- **Closed loop** keeps 500 requests in flight. That is more than the gateway can serve at once, so requests queue for CPU and the queueing time appears as "overhead". Use this mode for **capacity**, not for overhead.
- **Open loop** (`--rate`) sends a fixed request rate below capacity. That is the condition the <10 ms overhead target is about.

**CI.** Every PR runs the `compose-smoke` job on a GitHub-hosted runner. The full compose stack (gateway, Redis, both mocks, Jaeger) and the load generator all share one VM. The job prints the runner hardware before the load tests.

*Public-repo runner: 4 vCPU, 15 GiB, Intel Xeon 6975P-C, real Redis. Two runs on identical code:*

| Mode | Run | Throughput | Overhead p50 / p95 / p99 | Redis calls p50 / p95 / p99 |
| --- | --- | --- | --- | --- |
| Capacity: closed loop, 500 in flight, 5,000 requests | 1 | 408 req/s | 75 / 192 / 330 ms (queueing) | 33 / 83 / 173 ms |
| | 2 | **630 req/s** | 48 / 99 / 211 ms (queueing) | 25 / 53 / 93 ms |
| Overhead SLO: open loop at 100 req/s, 3,000 requests | 1 | 100 req/s | 1.77 / 2.98 / **4.73 ms** | 0.26 / 0.57 / 1.13 ms |
| | 2 | 100 req/s | 1.63 / 2.42 / **2.49 ms** | 0.26 / 0.49 / 0.90 ms |

Both runs had zero errors. The 429 responses come from the `search` team's 30 req/min limit, which the test hits on purpose.

**The <10 ms gateway-overhead target holds at p99 with Redis in the request path on the 4-vCPU runner.** Redis round trips account for about 1 ms of that.

On the earlier 2-vCPU private-repo runner, the same code gave p95 3.9–12.1 ms and p99 7.7–24.5 ms. That runner had too little CPU for six processes, so the tail was scheduling noise. CI therefore gates p95 at 25 ms, as a regression guard against those noisy runners.

Throughput in closed-loop mode is bounded by the shared VM, which also runs the load generator and mocks. It is a lower bound, not the gateway's ceiling. To scale further, add gateway replicas: all limit and budget state lives in Redis.

All values are interpolated within histogram buckets.

**Local.** Windows 11 laptop with the in-memory store, so there are no Redis round trips:

| Mode | Throughput | Overhead p50 / p95 / p99 |
| --- | --- | --- |
| Capacity, 300 in flight | 570 req/s | 0.28 / 0.80 / 0.98 ms |
| Open loop at 200 req/s | 200 req/s | 0.72 / 1.73 / 2.35 ms |
| Capacity, 500 in flight, primary outage halfway through | 388 req/s | 0.72 / 2.13 / 2.49 ms; 2,287 requests served by the fallback, zero 5xx |

**How throughput went from 83 to 630 req/s in CI** (83 and 463 on the 2-vCPU runner, 630 on the 4-vCPU one):

1. **httpx was the bottleneck.** Against the same mock provider, httpx fell from 204 to 89 req/s as concurrency rose from 10 to 200, while aiohttp reached 3,346 req/s. The gateway now calls providers through aiohttp (`app/http.py`).
2. **Admission is one Redis round trip.** Rate limiting and budget reservation run in a single Lua script.
3. **Lighter middleware.** A pure ASGI middleware replaced Starlette's `BaseHTTPMiddleware`.
4. **Trace sampling.** Head-based sampling uses `GATEWAY_OTEL_SAMPLE_RATIO`, set to 0.1 in compose. Span export holds the GIL in a background thread.

`gc.freeze()` also runs after startup.

To scale further, run more gateway replicas: Redis already holds all shared limit and budget state.
