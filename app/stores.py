"""Distributed state: token-bucket rate limits, daily spend ledger, runtime policy overrides.

Two implementations share one interface:
  * ``MemoryStore`` - single-process, used for tests/dev/the Space.
  * ``RedisStore``  - atomic Lua scripts; safe across many gateway replicas.

Money is tracked in integer micro-dollars to avoid float drift.
"""

from __future__ import annotations

import asyncio
import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

MICRO = 1_000_000


@dataclass
class RateDecision:
    allowed: bool
    retry_after_s: float = 0.0
    limit: str | None = None  # "requests" | "tokens"
    remaining_requests: float = 0.0
    remaining_tokens: float = 0.0


@dataclass
class BudgetDecision:
    allowed: bool
    spent_usd: float
    reserved_usd: float
    cap_usd: float

    @property
    def utilization(self) -> float:
        return (self.spent_usd + self.reserved_usd) / self.cap_usd if self.cap_usd else 1.0


def utc_day(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y-%m-%d")


def seconds_until_utc_midnight(now: datetime | None = None) -> float:
    now = now or datetime.now(UTC)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (tomorrow - now).total_seconds()


def _reserve_floor(capacity: float, reserve_ratio: float, priority: str) -> float:
    """Batch traffic must leave ``reserve_ratio`` of the bucket for realtime traffic."""
    return capacity * reserve_ratio if priority == "batch" else 0.0


class Store(ABC):
    @abstractmethod
    async def acquire(
        self, team: str, rpm: int, tpm: int, tokens: int, priority: str, reserve_ratio: float
    ) -> RateDecision: ...

    @abstractmethod
    async def reserve_budget(self, team: str, amount_usd: float, cap_usd: float) -> BudgetDecision: ...

    @abstractmethod
    async def settle_budget(self, team: str, reserved_usd: float, actual_usd: float) -> float:
        """Release a reservation and record actual spend. Returns spend so far today."""

    @abstractmethod
    async def budget_status(self, team: str) -> tuple[float, float]:
        """(spent_usd, reserved_usd) for today."""

    @abstractmethod
    async def rate_status(self, team: str, rpm: int, tpm: int) -> dict[str, float]: ...

    @abstractmethod
    async def get_overrides(self) -> dict[str, dict[str, Any]]: ...

    @abstractmethod
    async def set_overrides(self, team: str, patch: dict[str, Any] | None) -> None: ...

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        return None


# --------------------------------------------------------------------------- memory


class _Bucket:
    __slots__ = ("tokens", "updated")

    def __init__(self, capacity: float, now: float) -> None:
        self.tokens, self.updated = capacity, now

    def refill(self, capacity: float, now: float) -> None:
        rate = capacity / 60.0
        self.tokens = min(capacity, self.tokens + (now - self.updated) * rate)
        self.updated = now


class MemoryStore(Store):
    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._lock = asyncio.Lock()
        self._req: dict[str, _Bucket] = {}
        self._tok: dict[str, _Bucket] = {}
        self._spent: dict[tuple[str, str], int] = {}
        self._reserved: dict[tuple[str, str], int] = {}
        self._overrides: dict[str, dict[str, Any]] = {}

    async def acquire(self, team, rpm, tpm, tokens, priority, reserve_ratio) -> RateDecision:
        async with self._lock:
            now = self._clock()
            rb = self._req.setdefault(team, _Bucket(rpm, now))
            tb = self._tok.setdefault(team, _Bucket(tpm, now))
            rb.refill(rpm, now)
            tb.refill(tpm, now)
            if tokens > tpm:
                return RateDecision(False, 60.0, "tokens", rb.tokens, tb.tokens)
            r_need = 1 + _reserve_floor(rpm, reserve_ratio, priority)
            t_need = tokens + _reserve_floor(tpm, reserve_ratio, priority)
            if rb.tokens < r_need:
                return RateDecision(False, (r_need - rb.tokens) * 60.0 / rpm, "requests", rb.tokens, tb.tokens)
            if tb.tokens < t_need:
                return RateDecision(False, (t_need - tb.tokens) * 60.0 / tpm, "tokens", rb.tokens, tb.tokens)
            rb.tokens -= 1
            tb.tokens -= tokens
            return RateDecision(True, 0.0, None, rb.tokens, tb.tokens)

    async def rate_status(self, team, rpm, tpm) -> dict[str, float]:
        async with self._lock:
            now = self._clock()
            rb = self._req.setdefault(team, _Bucket(rpm, now))
            tb = self._tok.setdefault(team, _Bucket(tpm, now))
            rb.refill(rpm, now)
            tb.refill(tpm, now)
            return {"requests_remaining": rb.tokens, "tokens_remaining": tb.tokens}

    async def reserve_budget(self, team, amount_usd, cap_usd) -> BudgetDecision:
        key = (team, utc_day())
        amount, cap = int(amount_usd * MICRO), int(cap_usd * MICRO)
        async with self._lock:
            spent, reserved = self._spent.get(key, 0), self._reserved.get(key, 0)
            if spent + reserved + amount > cap:
                return BudgetDecision(False, spent / MICRO, reserved / MICRO, cap_usd)
            self._reserved[key] = reserved + amount
            return BudgetDecision(True, spent / MICRO, (reserved + amount) / MICRO, cap_usd)

    async def settle_budget(self, team, reserved_usd, actual_usd) -> float:
        key = (team, utc_day())
        async with self._lock:
            self._reserved[key] = max(0, self._reserved.get(key, 0) - int(reserved_usd * MICRO))
            self._spent[key] = self._spent.get(key, 0) + int(round(actual_usd * MICRO))
            return self._spent[key] / MICRO

    async def budget_status(self, team) -> tuple[float, float]:
        key = (team, utc_day())
        return self._spent.get(key, 0) / MICRO, self._reserved.get(key, 0) / MICRO

    async def get_overrides(self):
        return {k: dict(v) for k, v in self._overrides.items()}

    async def set_overrides(self, team, patch):
        if patch is None:
            self._overrides.pop(team, None)
        else:
            self._overrides[team] = {**self._overrides.get(team, {}), **patch}


# --------------------------------------------------------------------------- redis

# KEYS: req_bucket, tok_bucket. ARGV: rpm, tpm, tokens, req_floor, tok_floor
_ACQUIRE_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local rpm, tpm = tonumber(ARGV[1]), tonumber(ARGV[2])
local need_t, rfloor, tfloor = tonumber(ARGV[3]), tonumber(ARGV[4]), tonumber(ARGV[5])

local function load(key, cap)
  local v = redis.call('HMGET', key, 'tokens', 'ts')
  local tokens, ts = tonumber(v[1]), tonumber(v[2])
  if tokens == nil then return cap end
  return math.min(cap, tokens + math.max(0, now - ts) * cap / 60)
end

local r, k = load(KEYS[1], rpm), load(KEYS[2], tpm)
local allowed, limit, wait = 1, '', 0
if need_t > tpm then
  allowed, limit, wait = 0, 'tokens', 60
elseif r < 1 + rfloor then
  allowed, limit, wait = 0, 'requests', (1 + rfloor - r) * 60 / rpm
elseif k < need_t + tfloor then
  allowed, limit, wait = 0, 'tokens', (need_t + tfloor - k) * 60 / tpm
else
  r, k = r - 1, k - need_t
end
redis.call('HSET', KEYS[1], 'tokens', r, 'ts', now)
redis.call('HSET', KEYS[2], 'tokens', k, 'ts', now)
redis.call('EXPIRE', KEYS[1], 120)
redis.call('EXPIRE', KEYS[2], 120)
return {allowed, limit, tostring(wait), tostring(r), tostring(k)}
"""

# KEYS: spent, reserved. ARGV: amount, cap (micro-USD)
_RESERVE_LUA = """
local spent = tonumber(redis.call('GET', KEYS[1]) or '0')
local reserved = tonumber(redis.call('GET', KEYS[2]) or '0')
local amount, cap = tonumber(ARGV[1]), tonumber(ARGV[2])
if spent + reserved + amount > cap then return {0, spent, reserved} end
reserved = redis.call('INCRBY', KEYS[2], amount)
redis.call('EXPIRE', KEYS[2], 172800)
return {1, spent, reserved}
"""

# KEYS: spent, reserved. ARGV: reserved_amount, actual
_SETTLE_LUA = """
local r = redis.call('DECRBY', KEYS[2], ARGV[1])
if r < 0 then redis.call('SET', KEYS[2], 0) end
local s = redis.call('INCRBY', KEYS[1], ARGV[2])
redis.call('EXPIRE', KEYS[1], 172800)
redis.call('EXPIRE', KEYS[2], 172800)
return s
"""


class RedisStore(Store):
    OVERRIDES_KEY = "gw:policy:overrides"

    def __init__(self, url: str, prefix: str = "gw", max_connections: int = 200, pool_timeout_s: float = 2.0) -> None:
        import redis.asyncio as redis

        # Blocking pool: bursts above max_connections wait for a free connection instead of failing.
        pool = redis.BlockingConnectionPool.from_url(
            url,
            max_connections=max_connections,
            timeout=pool_timeout_s,
            decode_responses=True,
            socket_timeout=1.0,
            socket_connect_timeout=1.0,
        )
        self.r = redis.Redis(connection_pool=pool)
        self.p = prefix
        self._acquire = self.r.register_script(_ACQUIRE_LUA)
        self._reserve = self.r.register_script(_RESERVE_LUA)
        self._settle = self.r.register_script(_SETTLE_LUA)

    def _rk(self, team: str) -> list[str]:
        return [f"{self.p}:rl:{{{team}}}:req", f"{self.p}:rl:{{{team}}}:tok"]

    def _bk(self, team: str) -> list[str]:
        day = utc_day()
        return [f"{self.p}:budget:{{{team}}}:{day}:spent", f"{self.p}:budget:{{{team}}}:{day}:reserved"]

    async def acquire(self, team, rpm, tpm, tokens, priority, reserve_ratio) -> RateDecision:
        rfloor = _reserve_floor(rpm, reserve_ratio, priority)
        tfloor = _reserve_floor(tpm, reserve_ratio, priority)
        allowed, limit, wait, r, k = await self._acquire(keys=self._rk(team), args=[rpm, tpm, tokens, rfloor, tfloor])
        return RateDecision(bool(int(allowed)), float(wait), limit or None, float(r), float(k))

    async def rate_status(self, team, rpm, tpm) -> dict[str, float]:
        now = time.time()
        out = {}
        for key, cap, name in zip(self._rk(team), (rpm, tpm), ("requests_remaining", "tokens_remaining"), strict=True):
            tokens, ts = await self.r.hmget(key, "tokens", "ts")
            out[name] = cap if tokens is None else min(cap, float(tokens) + max(0.0, now - float(ts)) * cap / 60)
        return out

    async def reserve_budget(self, team, amount_usd, cap_usd) -> BudgetDecision:
        ok, spent, reserved = await self._reserve(
            keys=self._bk(team), args=[int(amount_usd * MICRO), int(cap_usd * MICRO)]
        )
        return BudgetDecision(bool(int(ok)), int(spent) / MICRO, int(reserved) / MICRO, cap_usd)

    async def settle_budget(self, team, reserved_usd, actual_usd) -> float:
        spent = await self._settle(
            keys=self._bk(team), args=[int(reserved_usd * MICRO), int(round(actual_usd * MICRO))]
        )
        return int(spent) / MICRO

    async def budget_status(self, team) -> tuple[float, float]:
        spent, reserved = await self.r.mget(self._bk(team))
        return int(spent or 0) / MICRO, max(0, int(reserved or 0)) / MICRO

    async def get_overrides(self):
        raw = await self.r.hgetall(self.OVERRIDES_KEY)
        return {team: json.loads(v) for team, v in raw.items()}

    async def set_overrides(self, team, patch):
        if patch is None:
            await self.r.hdel(self.OVERRIDES_KEY, team)
            return
        current = await self.r.hget(self.OVERRIDES_KEY, team)
        merged = {**(json.loads(current) if current else {}), **patch}
        await self.r.hset(self.OVERRIDES_KEY, team, json.dumps(merged))

    async def ping(self) -> bool:
        try:
            return bool(await self.r.ping())
        except Exception:
            return False

    async def close(self) -> None:
        await self.r.aclose(close_connection_pool=True)
