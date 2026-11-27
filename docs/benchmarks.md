# Benchmarks

How the gateway was load-tested and what came out. The headline numbers are in the [README](../README.md#results).

## How to run

```bash
python loadtests/loadtest.py --requests 5000 --concurrency 500 [--outage]   # capacity: keeps 500 requests in flight
python loadtests/loadtest.py --requests 3000 --rate 100                     # overhead: a steady 100 requests per second
k6 run loadtests/gateway.js                                                   # a sustained load profile
```

`loadtest.py` reads two timers that the gateway records itself:

- `gateway_overhead_seconds`: the time a request spends inside the gateway. It leaves out the model's own time and any retry waits.
- `gateway_store_duration_seconds`: the time spent talking to Redis. This is part of the overhead.

Neither number includes the time the model takes to answer.

## Two kinds of test

**Capacity** keeps 500 requests in flight at once. That is more than the gateway can work on at the same moment, so requests wait in line for the CPU, and that waiting shows up as "overhead". Use this test to find the most requests per second the setup can handle, not to judge overhead.

**Overhead** sends a steady 100 requests per second, below capacity. This is the situation the "under 10 ms" target is about.

## Results in CI

Every pull request runs the `compose-smoke` job on a GitHub runner. The whole stack (gateway, Redis, two mock models, Jaeger) and the load generator share one machine. The job prints the machine's hardware before the tests.

Machine: 4 vCPU, 15 GiB, Intel Xeon 6975P-C, real Redis. Two runs of the same code:

| Test | Run | Requests per second | Overhead p50 / p95 / p99 | Redis time p50 / p95 / p99 |
| --- | --- | --- | --- | --- |
| Capacity: 500 in flight, 5,000 requests | 1 | 408 | 75 / 192 / 330 ms (mostly waiting in line) | 33 / 83 / 173 ms |
| | 2 | **630** | 48 / 99 / 211 ms (mostly waiting in line) | 25 / 53 / 93 ms |
| Overhead: steady 100 per second, 3,000 requests | 1 | 100 | 1.77 / 2.98 / **4.73 ms** | 0.26 / 0.57 / 1.13 ms |
| | 2 | 100 | 1.63 / 2.42 / **2.49 ms** | 0.26 / 0.49 / 0.90 ms |

Both runs had zero errors. Some `429` responses are expected: the test deliberately pushes the `search` team past its limit of 30 requests a minute.

**The gateway stays under 10 ms of overhead at p99 with Redis in the path.** About 1 ms of that is Redis.

An earlier 2-vCPU runner gave p95 of 3.9 to 12.1 ms and p99 of 7.7 to 24.5 ms with the same code. Six processes shared two CPUs there, so the slow tail was the machine, not the gateway. CI therefore fails a build only if p95 goes above 25 ms. That catches real slowdowns without failing on a noisy machine.

The capacity numbers are a floor, not a ceiling: the load generator and mocks run on the same machine. To handle more traffic, run more copies of the gateway. All limits and budgets live in Redis, so the copies stay in sync.

The percentiles are estimated from histogram buckets.

## Results on a laptop

Windows 11 laptop, counters kept in memory instead of Redis:

| Test | Requests per second | Overhead p50 / p95 / p99 |
| --- | --- | --- |
| Capacity, 300 in flight | 570 | 0.28 / 0.80 / 0.98 ms |
| Steady 200 per second | 200 | 0.72 / 1.73 / 2.35 ms |
| Capacity, 500 in flight, main model taken down halfway | 388 | 0.72 / 2.13 / 2.49 ms; the fallback served 2,287 requests, with no 5xx errors |

## How throughput went from 83 to 630 requests per second

83 and 463 were measured on the 2-vCPU runner; 630 on the 4-vCPU one.

1. **The HTTP client was the bottleneck.** Against the same mock model, httpx dropped from 204 to 89 requests per second as concurrency rose from 10 to 200. aiohttp reached 3,346. The gateway now calls models through aiohttp (`app/http.py`).
2. **One trip to Redis per request.** The rate-limit check and the budget reservation run together in a single Lua script.
3. **Lighter middleware.** A plain ASGI middleware replaced Starlette's `BaseHTTPMiddleware`.
4. **Fewer traces.** Only a share of requests is traced, set by `GATEWAY_OTEL_SAMPLE_RATIO` (0.1 in Docker Compose). Exporting traces holds Python's global lock, so tracing every request slowed everything down.

The gateway also calls `gc.freeze()` after startup, so the garbage collector skips objects created at startup.
