import asyncio

import pytest


async def test_token_bucket_request_limit_and_retry_after(store):
    results = [await store.acquire("t1", 5, 10_000, 10, "realtime", 0.0) for _ in range(6)]
    assert [r.allowed for r in results] == [True] * 5 + [False]
    denied = results[-1]
    assert denied.limit == "requests"
    assert 0 < denied.retry_after_s <= 12.5  # one request refills in 60/5 s


async def test_token_limit(store):
    assert (await store.acquire("t2", 100, 1000, 600, "realtime", 0.0)).allowed
    d = await store.acquire("t2", 100, 1000, 600, "realtime", 0.0)
    assert not d.allowed and d.limit == "tokens"


async def test_request_larger_than_bucket_is_rejected(store):
    d = await store.acquire("t3", 100, 1000, 5000, "realtime", 0.0)
    assert not d.allowed and d.limit == "tokens"


async def test_batch_cannot_drain_realtime_reserve(store):
    # rpm=10 with 30% reserve: batch may use 7 requests, realtime can still use the remaining 3.
    batch = [await store.acquire("t4", 10, 1_000_000, 1, "batch", 0.3) for _ in range(10)]
    assert sum(r.allowed for r in batch) == 7
    realtime = [await store.acquire("t4", 10, 1_000_000, 1, "realtime", 0.3) for _ in range(4)]
    assert sum(r.allowed for r in realtime) == 3


async def test_rate_limit_is_exact_under_concurrency(store):
    results = await asyncio.gather(*(store.acquire("t5", 50, 1_000_000, 1, "realtime", 0.0) for _ in range(200)))
    assert sum(r.allowed for r in results) == 50


async def test_budget_reserve_settle_and_block(store):
    d = await store.reserve_budget("b1", 0.6, 1.0)
    assert d.allowed and d.reserved_usd == pytest.approx(0.6)
    assert not (await store.reserve_budget("b1", 0.6, 1.0)).allowed  # reservation counts against cap
    spent = await store.settle_budget("b1", 0.6, 0.25)
    assert spent == pytest.approx(0.25)
    assert await store.budget_status("b1") == (pytest.approx(0.25), pytest.approx(0.0))
    assert (await store.reserve_budget("b1", 0.7, 1.0)).allowed
    await store.settle_budget("b1", 0.7, 0.75)
    blocked = await store.reserve_budget("b1", 0.000001, 1.0)
    assert not blocked.allowed and blocked.spent_usd == pytest.approx(1.0)


async def test_concurrent_budget_reservations_never_exceed_cap(store):
    results = await asyncio.gather(*(store.reserve_budget("b2", 0.1, 1.0) for _ in range(50)))
    assert sum(r.allowed for r in results) == 10


async def test_overrides_roundtrip(store):
    await store.set_overrides("x", {"requests_per_minute": 5})
    await store.set_overrides("x", {"daily_budget_usd": 2.0})
    assert await store.get_overrides() == {"x": {"requests_per_minute": 5, "daily_budget_usd": 2.0}}
    await store.set_overrides("x", None)
    assert await store.get_overrides() == {}


async def test_admit_budget_rejection_does_not_consume_rate_tokens(store):
    rate, budget = await store.admit("a1", 2, 1000, 10, "realtime", 0.0, 5.0, 1.0)
    assert not rate.allowed and rate.limit == "budget" and budget is not None and not budget.allowed
    # Both request tokens are still available.
    assert all([(await store.admit("a1", 2, 1000, 10, "realtime", 0.0, 0.1, 1.0))[0].allowed for _ in range(2)])
    assert not (await store.admit("a1", 2, 1000, 10, "realtime", 0.0, 0.1, 1.0))[0].allowed


async def test_admit_rate_rejection_reserves_no_budget(store):
    await store.admit("a2", 1, 1000, 10, "realtime", 0.0, 0.1, 1.0)
    rate, budget = await store.admit("a2", 1, 1000, 10, "realtime", 0.0, 0.1, 1.0)
    assert not rate.allowed and rate.limit == "requests" and budget is None
    assert await store.budget_status("a2") == (pytest.approx(0.0), pytest.approx(0.1))


async def test_admit_exact_under_concurrency(store):
    results = await asyncio.gather(*(store.admit("a3", 1000, 10**6, 1, "realtime", 0.0, 0.1, 1.0) for _ in range(50)))
    assert sum(r.allowed for r, _ in results) == 10
