"""Concurrent load test with mixed teams, tiers and priorities; reports measured numbers only.

    python loadtests/loadtest.py --url http://localhost:8000 --requests 5000 --concurrency 500 [--outage]
    python loadtests/loadtest.py --requests 3000 --rate 100     # fixed arrival rate, for latency/overhead SLOs

Closed loop (default) finds capacity: once the gateway saturates, extra in-flight requests wait for CPU
and that queueing shows up as overhead. Measure the overhead SLO with --rate below capacity.

Gateway overhead is read from the gateway's own `gateway_overhead_seconds` histogram (time spent in the
gateway excluding provider calls), so it is independent of mock-provider latency.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import statistics
import time
from collections import Counter

import aiohttp

PROFILES = [
    # (weight, key, tier, priority)
    (60, "loadtest-key", "balanced", "realtime"),
    (15, "loadtest-key", "economy", "batch"),
    (10, "loadtest-key", "premium", "realtime"),
    (10, "search-live-key", "economy", "realtime"),  # tight limits: expect 429s
    (5, "demo-live-key", "balanced", "realtime"),
]


def pick() -> tuple[str, str, str]:
    _, key, tier, prio = random.choices(PROFILES, weights=[p[0] for p in PROFILES])[0]
    return key, tier, prio


def histogram(text: str, name: str) -> list[tuple[float, float]]:
    out = []
    for m in re.finditer(rf'^{name}_bucket\{{le="([^"]+)"\}} ([0-9.e+]+)$', text, re.M):
        le = float("inf") if m.group(1) == "+Inf" else float(m.group(1))
        out.append((le, float(m.group(2))))
    return sorted(out)


def quantile(buckets: list[tuple[float, float]], q: float) -> float | None:
    if not buckets or buckets[-1][1] == 0:
        return None
    target, prev_le, prev_c = q * buckets[-1][1], 0.0, 0.0
    for le, c in buckets:
        if c >= target:
            if le == float("inf"):
                return prev_le
            return prev_le + (le - prev_le) * ((target - prev_c) / max(c - prev_c, 1e-12))
        prev_le, prev_c = le, c
    return None


def diff(after, before):
    b = dict(before)
    return [(le, c - b.get(le, 0.0)) for le, c in after]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--requests", type=int, default=5000)
    ap.add_argument("--concurrency", type=int, default=500)
    ap.add_argument("--outage", action="store_true", help="simulate a mock-primary outage halfway through")
    ap.add_argument("--admin-key", default="demo-admin-key-change-me-0000")
    ap.add_argument("--max-overhead-p99-ms", type=float, help="exit 1 if gateway overhead p99 exceeds this")
    ap.add_argument(
        "--rate",
        type=float,
        default=0.0,
        help="open-loop arrival rate in req/s (0 = closed loop: as fast as --concurrency allows)",
    )
    args = ap.parse_args()

    connector = aiohttp.TCPConnector(limit=args.concurrency)
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(base_url=args.url, connector=connector, timeout=timeout) as client:

        async def metrics_text() -> str:
            async with client.get("/metrics") as r:
                return await r.text()

        async def set_outage(enabled: bool) -> None:
            async with client.post(
                "/v1/admin/providers/mock-primary/simulate-outage",
                params={"enabled": str(enabled).lower()},
                headers={"X-Admin-Key": args.admin_key},
            ) as r:
                r.raise_for_status()

        before = histogram(await metrics_text(), "gateway_overhead_seconds")
        statuses: Counter[str] = Counter()
        providers: Counter[str] = Counter()
        latencies: list[float] = []
        sem = asyncio.Semaphore(args.concurrency)
        done = 0

        async def one(i: int) -> None:
            nonlocal done
            key, tier, prio = pick()
            body = {
                "model": tier,
                "priority": prio,
                "max_tokens": 64,
                "messages": [{"role": "user", "content": f"load test request {i}"}],
            }
            if args.rate:
                await asyncio.sleep(max(0.0, t_start + i / args.rate - time.perf_counter()))
            async with sem:
                t0 = time.perf_counter()
                try:
                    async with client.post("/v1/chat/completions", json=body, headers={"X-API-Key": key}) as r:
                        await r.read()
                        statuses[str(r.status)] += 1
                        if r.status == 200:
                            providers[r.headers.get("X-Gateway-Provider", "?")] += 1
                            latencies.append(time.perf_counter() - t0)
                except (aiohttp.ClientError, TimeoutError) as exc:
                    statuses[type(exc).__name__] += 1
            done += 1
            if args.outage and done == args.requests // 2:
                await set_outage(True)

        t0 = t_start = time.perf_counter()
        await asyncio.gather(*(one(i) for i in range(args.requests)))
        wall = time.perf_counter() - t0
        if args.outage:
            await set_outage(False)
        after = histogram(await metrics_text(), "gateway_overhead_seconds")

    over = diff(after, before)
    latencies.sort()

    def pct(q):
        return round(latencies[min(len(latencies) - 1, int(q * len(latencies)))] * 1000, 1) if latencies else None

    report = {
        "requests": args.requests,
        "concurrency": args.concurrency,
        "mode": f"open loop @ {args.rate:g} req/s" if args.rate else "closed loop",
        "wall_seconds": round(wall, 2),
        "throughput_rps": round(args.requests / wall, 1),
        "status_counts": dict(statuses),
        "served_by": dict(providers),
        "client_latency_ms": {
            "p50": pct(0.5),
            "p95": pct(0.95),
            "p99": pct(0.99),
            "mean": round(statistics.fmean(latencies) * 1000, 1) if latencies else None,
        },
        "gateway_overhead_ms": {
            f"p{int(q * 100)}": (round(v * 1000, 2) if (v := quantile(over, q)) is not None else None)
            for q in (0.5, 0.95, 0.99)
        },
    }
    print(json.dumps(report, indent=2))
    p99 = report["gateway_overhead_ms"]["p99"]
    if args.max_overhead_p99_ms is not None and (p99 is None or p99 > args.max_overhead_p99_ms):
        raise SystemExit(f"gateway overhead p99 {p99} ms exceeds {args.max_overhead_p99_ms} ms")


if __name__ == "__main__":
    asyncio.run(main())
