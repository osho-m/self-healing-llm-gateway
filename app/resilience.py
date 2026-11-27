"""Circuit breakers, rolling provider health, and fault injection."""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"
STATE_VALUE = {CLOSED: 0, HALF_OPEN: 1, OPEN: 2}


class CircuitBreaker:
    """Opens after ``threshold`` failures within ``window_s``; after ``cooldown_s`` admits exactly one probe."""

    def __init__(
        self,
        name: str,
        threshold: int,
        window_s: float,
        cooldown_s: float,
        on_change: Callable[[str, str, str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        on_local_open: Callable[[str], None] | None = None,
    ) -> None:
        self.name, self.threshold, self.window_s, self.cooldown_s = name, threshold, window_s, cooldown_s
        self._on_change, self._clock = on_change, clock
        # Called only when THIS breaker opens from its own failures, never when it adopts an open
        # circuit from another replica. Used to share opens without echoing them back.
        self.on_local_open = on_local_open
        self.state = CLOSED
        self._failures: deque[float] = deque()
        self._opened_at = 0.0
        self._probe_in_flight = False

    def configure(self, threshold: int, window_s: float, cooldown_s: float) -> None:
        self.threshold, self.window_s, self.cooldown_s = threshold, window_s, cooldown_s

    def _transition(self, new: str) -> None:
        if new == self.state:
            return
        old, self.state = self.state, new
        log.warning("circuit state change", extra={"provider": self.name, "from": old, "to": new})
        if self._on_change:
            self._on_change(self.name, old, new)

    def current_state(self) -> str:
        if self.state == OPEN and self._clock() - self._opened_at >= self.cooldown_s:
            self._transition(HALF_OPEN)
            self._probe_in_flight = False
        return self.state

    def allow(self) -> bool:
        """Reserve permission to call. In half-open only one caller gets through."""
        state = self.current_state()
        if state == CLOSED:
            return True
        if state == HALF_OPEN and not self._probe_in_flight:
            self._probe_in_flight = True
            return True
        return False

    def record_success(self) -> None:
        self._failures.clear()
        self._probe_in_flight = False
        self._transition(CLOSED)

    def _open_locally(self, now: float) -> None:
        self._opened_at = now
        self._transition(OPEN)
        if self.on_local_open:
            self.on_local_open(self.name)

    def record_failure(self) -> None:
        now = self._clock()
        self._probe_in_flight = False
        if self.state == HALF_OPEN:
            self._open_locally(now)
            return
        self._failures.append(now)
        while self._failures and self._failures[0] < now - self.window_s:
            self._failures.popleft()
        if self.state == CLOSED and len(self._failures) >= self.threshold:
            self._open_locally(now)

    def adopt_open(self, remaining_s: float) -> None:
        """Open because another replica opened this circuit, ``remaining_s`` before its cooldown ends.

        Only the time left is shared (monotonic clocks differ between machines), so every replica
        goes half-open at about the same moment. A circuit that is already open is left alone.
        """
        if remaining_s <= 0 or self.current_state() == OPEN:
            return
        self._failures.clear()
        self._probe_in_flight = False
        # Half-open at now + remaining_s, even when that is longer than this replica's own cooldown.
        self._opened_at = self._clock() - (self.cooldown_s - remaining_s)
        self._transition(OPEN)

    def release(self) -> None:
        """Release a half-open probe slot without recording an outcome (e.g. non-retryable client error)."""
        self._probe_in_flight = False


@dataclass
class HealthTracker:
    """Rolling-window error rate and latency percentiles for one provider."""

    name: str
    window_s: float = 300.0
    samples: deque = field(default_factory=deque)  # (ts, ok, latency_ms)
    history: deque = field(default_factory=lambda: deque(maxlen=500))  # (wall ts, status, reason)
    status: str = "healthy"
    last_probe_at: float | None = None
    last_error: str | None = None

    def record(self, ok: bool, latency_ms: float, error: str | None = None, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self.samples.append((now, ok, latency_ms))
        if not ok:
            self.last_error = error
        self._trim(now)

    def _trim(self, now: float) -> None:
        while self.samples and self.samples[0][0] < now - self.window_s:
            self.samples.popleft()

    def stats(self) -> dict:
        self._trim(time.monotonic())
        n = len(self.samples)
        if not n:
            return {"samples": 0, "error_rate": 0.0, "p50_ms": None, "p99_ms": None}
        errors = sum(1 for _, ok, _ in self.samples if not ok)
        lat = sorted(s[2] for s in self.samples if s[1])
        return {
            "samples": n,
            "error_rate": errors / n,
            "p50_ms": _pct(lat, 0.50),
            "p99_ms": _pct(lat, 0.99),
        }

    def evaluate(self, degraded_error_rate: float, down_error_rate: float, degraded_p99_ms: float, circuit: str) -> str:
        s = self.stats()
        if circuit == OPEN or (s["samples"] >= 3 and s["error_rate"] >= down_error_rate):
            status, reason = "down", f"circuit={circuit} error_rate={s['error_rate']:.2f}"
        elif circuit == HALF_OPEN or s["error_rate"] >= degraded_error_rate or (s["p99_ms"] or 0) > degraded_p99_ms:
            status, reason = "degraded", f"error_rate={s['error_rate']:.2f} p99={s['p99_ms']}"
        else:
            status, reason = "healthy", "ok"
        if status != self.status:
            self.history.append((time.time(), status, reason))
            log.warning("provider health change", extra={"provider": self.name, "from": self.status, "to": status})
            self.status = status
        return status


def _pct(sorted_vals: list[float], q: float) -> float | None:
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return round(sorted_vals[idx], 2)


@dataclass
class Fault:
    outage: bool = False
    latency_ms: float = 0.0


class FaultInjector:
    """Gateway-side chaos controls used by the admin API and demos. Never enabled by default."""

    def __init__(self) -> None:
        self.faults: dict[str, Fault] = {}

    def get(self, provider: str) -> Fault:
        return self.faults.get(provider) or Fault()

    def set(self, provider: str, *, outage: bool | None = None, latency_ms: float | None = None) -> Fault:
        f = self.faults.setdefault(provider, Fault())
        if outage is not None:
            f.outage = outage
        if latency_ms is not None:
            f.latency_ms = max(0.0, latency_ms)
        return f
