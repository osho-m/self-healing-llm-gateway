"""Self-contained no-key gateway engine used by the public Gradio Space."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass


@dataclass
class Circuit:
    failures: int = 0
    state: str = "closed"
    opened_at: float | None = None

    def available(self) -> bool:
        if self.state == "open" and time.monotonic() - (self.opened_at or 0) >= 15:
            self.state = "half_open"
        return self.state != "open"

    def success(self) -> None:
        self.failures, self.state, self.opened_at = 0, "closed", None

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= 3:
            self.state, self.opened_at = "open", time.monotonic()


class DemoGateway:
    """Deterministic primary/fallback routing with budget and token-window policy."""
    prices = {"economy": (0.03, 0.12), "balanced": (0.15, 0.60), "premium": (2.50, 10.0)}

    def __init__(self) -> None:
        self.primary_outage, self.spend = False, 0.0
        self.circuits = {"mock-primary": Circuit(), "mock-secondary": Circuit()}
        self.events: deque[tuple[float, int]] = deque()

    def set_outage(self, enabled: bool) -> str:
        self.primary_outage = enabled
        return "OUTAGE ENABLED — requests will retry then fail over." if enabled else "Primary restored — circuit recovery begins after cooldown."

    def health(self) -> dict:
        return {"primary_outage": self.primary_outage, "circuits": {name: circuit.state for name, circuit in self.circuits.items()}, "spend_usd": round(self.spend, 8)}

    def _allow(self, estimated_tokens: int) -> None:
        now = time.monotonic()
        while self.events and self.events[0][0] < now - 60:
            self.events.popleft()
        if len(self.events) >= 60 or sum(tokens for _, tokens in self.events) + estimated_tokens > 40_000:
            raise RuntimeError("Demo team rate limit reached; retry in a minute.")
        self.events.append((now, estimated_tokens))

    def complete(self, prompt: str, tier: str) -> tuple[str, dict]:
        if tier not in self.prices:
            raise ValueError("Unknown capability tier")
        self._allow(max(1, len(prompt) // 4) + 512)
        start, attempts, provider = time.perf_counter(), [], "mock-primary"
        primary = self.circuits[provider]
        if primary.available():
            for _ in range(3):
                if not self.primary_outage:
                    primary.success()
                    break
                primary.failure()
                attempts.append("mock-primary simulated outage")
            else:
                provider = "mock-secondary"
        else:
            attempts.append("mock-primary circuit open")
            provider = "mock-secondary"
        fallback = provider == "mock-secondary"
        if fallback:
            secondary = self.circuits[provider]
            if not secondary.available():
                raise RuntimeError("No healthy provider available")
            secondary.success()
        content = f"[{provider}/{tier}] {prompt[:512]}"
        input_tokens, output_tokens = max(1, len(prompt) // 4), max(1, len(content) // 4)
        input_price, output_price = self.prices[tier]
        cost = (input_tokens * input_price + output_tokens * output_price) / 1_000_000
        self.spend += cost
        return content, {"tier": tier, "provider": provider, "fallback_used": fallback, "attempts": attempts, "estimated_cost_usd": round(cost, 8), "latency_ms": round((time.perf_counter() - start) * 1000, 2), "usage": {"prompt_tokens": input_tokens, "completion_tokens": output_tokens, "total_tokens": input_tokens + output_tokens}}


gateway = DemoGateway()
