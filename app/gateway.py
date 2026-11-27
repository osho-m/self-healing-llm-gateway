"""Core request pipeline: policy -> enrichment -> rate limit -> budget -> routing -> settle -> telemetry."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from typing import Any

from app.audit import AuditLog
from app.errors import GatewayError
from app.http import AiohttpClient
from app.observability import HEALTH_STATUSES, Metrics, tracer
from app.policy import PolicyManager, TeamPolicy
from app.providers import CompletionRequest, CompletionResult, Provider, ProviderError, StreamChunk, build_provider
from app.providers.base import estimate_tokens
from app.resilience import CLOSED, HALF_OPEN, STATE_VALUE, CircuitBreaker, FaultInjector, HealthTracker
from app.schemas import ChatMessage, ChatRequest, ChatResponse, Choice, GatewayMeta, Usage
from app.stores import Store, seconds_until_utc_midnight

log = logging.getLogger(__name__)
_BACKGROUND_TASKS: set[asyncio.Task] = set()  # strong refs so close tasks are not GC'd mid-flight


@dataclass
class Admission:
    request_id: str
    team: TeamPolicy
    req: ChatRequest
    creq: CompletionRequest
    reserved_usd: float
    started: float
    rate_headers: dict[str, str]
    # max_tokens each provider in the chain was admitted with (its min_max_tokens floor applied at
    # admission time). Calls use these, so a later policy reload cannot exceed the reservation.
    output_caps: dict[str, int] = field(default_factory=dict)
    # Cost a provider billed for a call that failed without retry (e.g. a refusal); settled instead of 0.
    failed_cost_usd: float = 0.0
    provider_time: float = 0.0
    attempts: list[str] = field(default_factory=list)


class Gateway:
    def __init__(
        self,
        policy: PolicyManager,
        store: Store,
        metrics: Metrics,
        audit: AuditLog,
        client: Any = None,
        sleep=asyncio.sleep,
    ) -> None:
        self.policy_manager, self.store, self.metrics, self.audit = policy, store, metrics, audit
        # aiohttp in production (scales with concurrency); tests inject httpx clients with MockTransport.
        self.client = client or AiohttpClient()
        self.faults = FaultInjector()
        self._sleep = sleep
        self.providers: dict[str, Provider] = {}
        self.breakers: dict[str, CircuitBreaker] = {}
        self.health: dict[str, HealthTracker] = {}
        self._policy_version = -1
        self.sync_providers()

    # ------------------------------------------------------------------ policy / providers

    @property
    def policy(self):
        return self.policy_manager.policy

    def sync_providers(self) -> None:
        """(Re)build adapters after a policy change; keep breaker/health state for providers that still exist."""
        if self._policy_version == self.policy_manager.version:
            return
        res = self.policy.resilience
        for cfg in self.policy.providers:
            old = self.providers.get(cfg.name)
            if old is None or old.config != cfg:
                self.providers[cfg.name] = build_provider(cfg, self.client)
                if old is not None:
                    self._close_later(old)
            if not cfg.is_configured():
                log.warning(
                    "provider not configured; it will be skipped",
                    extra={"provider": cfg.name, "missing_env": cfg.api_key_env},
                )
            if cfg.name in self.breakers:
                self.breakers[cfg.name].configure(
                    res.circuit_failure_threshold, res.circuit_window_s, res.circuit_cooldown_s
                )
            else:
                self.breakers[cfg.name] = CircuitBreaker(
                    cfg.name,
                    res.circuit_failure_threshold,
                    res.circuit_window_s,
                    res.circuit_cooldown_s,
                    self._on_circuit,
                )
                self.metrics.circuit_state.labels(cfg.name).set(0)
            tracker = self.health.setdefault(cfg.name, HealthTracker(cfg.name))
            tracker.window_s = res.health_window_s
            self._set_health_gauge(cfg.name, tracker.status)
        for gone in set(self.providers) - {p.name for p in self.policy.providers}:
            self._close_later(self.providers.pop(gone))
        self._policy_version = self.policy_manager.version

    @staticmethod
    def _close_later(provider: Provider) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # no running loop (startup or tests); nothing to schedule on
            return
        task = loop.create_task(provider.retire())
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)

    def _configured(self, name: str) -> bool:
        return self.policy.provider(name).is_configured()

    async def refresh_policy(self) -> None:
        self.policy_manager.reload_if_changed()
        try:
            self.policy_manager.set_overrides(await self.store.get_overrides())
        except Exception:
            log.exception("could not apply runtime overrides")
        self.sync_providers()

    def _on_circuit(self, provider: str, old: str, new: str) -> None:
        self.metrics.circuit_state.labels(provider).set(STATE_VALUE[new])
        self.metrics.circuit_changes.labels(provider, new).inc()
        self._evaluate_health(provider)

    def _set_health_gauge(self, provider: str, status: str) -> None:
        for s in HEALTH_STATUSES:
            self.metrics.provider_health.labels(provider, s).set(1 if s == status else 0)

    def _evaluate_health(self, provider: str) -> str:
        res = self.policy.resilience
        status = self.health[provider].evaluate(
            res.degraded_error_rate, res.down_error_rate, res.degraded_p99_ms, self.breakers[provider].current_state()
        )
        self._set_health_gauge(provider, status)
        return status

    # ------------------------------------------------------------------ admission

    def _enrich(self, team: TeamPolicy, req: ChatRequest) -> list[dict[str, str]]:
        cfg = team.enrichment
        if cfg.blocked_terms:
            text = " ".join(m.content.lower() for m in req.messages)
            hit = next((t for t in cfg.blocked_terms if t.lower() in text), None)
            if hit:
                raise GatewayError(400, "content_policy_violation", "Request blocked by your team's content policy.")
        messages = [m.model_dump() for m in req.messages]
        if cfg.system_prompt:
            messages.insert(0, {"role": "system", "content": cfg.system_prompt})
        return messages

    def _chain_configs(self, tier: str) -> list:
        """Providers that can actually serve this tier (falls back to all if none are configured)."""
        chain = [self.policy.provider(p) for p in self.policy.tiers[tier]]
        return [c for c in chain if c.is_configured()] or chain

    def _max_cost(self, tier: str, prompt_tokens: int, max_tokens: int) -> float:
        # Each provider may raise max_tokens to its floor (min_max_tokens); reserve for that.
        return max(c.cost_usd(prompt_tokens, max(max_tokens, c.min_max_tokens)) for c in self._chain_configs(tier))

    def _output_caps(self, tier: str, max_tokens: int) -> dict[str, int]:
        return {c.name: max(max_tokens, c.min_max_tokens) for c in self._chain_configs(tier)}

    async def admit(self, team: TeamPolicy, req: ChatRequest) -> Admission:
        started = time.perf_counter()
        rid = uuid.uuid4().hex
        if req.model not in self.policy.tiers:
            raise GatewayError(404, "model_not_found", f"Unknown tier {req.model!r}. Use GET /v1/models.")
        if req.model not in team.allowed_tiers:
            raise GatewayError(403, "tier_not_allowed", f"Team {team.team_id!r} may not use tier {req.model!r}.")
        messages = self._enrich(team, req)
        prompt_est = sum(estimate_tokens(m["content"]) for m in messages)
        output_caps = self._output_caps(req.model, req.max_tokens)
        token_need = prompt_est + max(output_caps.values())

        estimate = self._max_cost(req.model, prompt_est, req.max_tokens)
        with tracer.start_as_current_span("gateway.admission") as span, self.metrics.store_duration.time():
            decision, budget = await self.store.admit(
                team.team_id,
                team.requests_per_minute,
                team.tokens_per_minute,
                token_need,
                req.priority,
                team.batch_reserve_ratio,
                estimate,
                team.daily_budget_usd,
            )
            span.set_attribute("gateway.allowed", decision.allowed)
        if budget is None:
            self.metrics.rate_limited.labels(team.team_id, decision.limit or "unknown", req.priority).inc()
            msg = (
                f"Rate limit exceeded ({decision.limit} per minute) for team {team.team_id!r}."
                if token_need <= team.tokens_per_minute
                else f"Request needs {token_need} tokens; team limit is {team.tokens_per_minute}/min. Lower max_tokens."
            )
            if req.priority == "batch":
                msg += " Batch traffic cannot use the capacity reserved for realtime requests."
            raise GatewayError(429, "rate_limit_exceeded", msg, retry_after=decision.retry_after_s)
        self.metrics.budget_spend.labels(team.team_id).set(budget.spent_usd)
        if not budget.allowed:
            self.metrics.rate_limited.labels(team.team_id, "budget", req.priority).inc()
            raise GatewayError(
                429,
                "budget_exceeded",
                f"Daily budget cap of ${team.daily_budget_usd:.2f} reached for team {team.team_id!r} "
                f"(spent ${budget.spent_usd:.4f}). Resets at 00:00 UTC; an admin can raise the cap.",
                retry_after=seconds_until_utc_midnight(),
            )
        rate_headers = {
            "X-RateLimit-Remaining-Requests": str(int(decision.remaining_requests)),
            "X-RateLimit-Remaining-Tokens": str(int(decision.remaining_tokens)),
        }
        creq = CompletionRequest(messages=messages, max_tokens=req.max_tokens, temperature=req.temperature)
        return Admission(rid, team, req, creq, estimate, started, rate_headers, output_caps)

    # ------------------------------------------------------------------ provider invocation

    def _sized(self, name: str, creq: CompletionRequest, caps: dict[str, int] | None) -> CompletionRequest:
        """Size max_tokens for one provider.

        Admitted requests use the cap admission reserved for that provider (routing skips providers
        that were not part of the admission snapshot). Calls outside admission (health probes) apply
        the provider's current floor.
        """
        if caps is not None:
            return replace(creq, max_tokens=caps[name])
        tokens = max(creq.max_tokens, self.policy.provider(name).min_max_tokens)
        return creq if tokens == creq.max_tokens else replace(creq, max_tokens=tokens)

    async def _invoke(self, name: str, creq: CompletionRequest, caps: dict[str, int] | None = None) -> CompletionResult:
        fault = self.faults.get(name)
        if fault.latency_ms:
            await asyncio.sleep(fault.latency_ms / 1000)
        if fault.outage:
            raise ProviderError(name, "server_error", "simulated outage", retryable=True, status=503)
        adapter = self.providers[name]
        adapter.acquire()
        try:
            return await adapter.complete(self._sized(name, creq, caps))
        except ProviderError as err:
            self._price_usage(adapter, err)
            raise
        finally:
            await adapter.release()

    def _backoff(self, attempt: int) -> float:
        res = self.policy.resilience
        return min(res.backoff_max_s, res.backoff_base_s * (2**attempt)) * random.uniform(0.5, 1.0)

    def _record(self, name: str, ok: bool, seconds: float, err: ProviderError | None = None) -> None:
        self.health[name].record(ok, seconds * 1000, None if err is None else f"{err.kind}: {err}")
        self.metrics.provider_latency.labels(name).observe(seconds)
        self._evaluate_health(name)

    @staticmethod
    def _price_usage(adapter: Provider, err: ProviderError) -> None:
        # Price billed usage with the invoked adapter's own config: a reload during the call may
        # have removed the provider or changed its prices.
        if err.usage:
            err.billed_usd = adapter.config.cost_usd(*err.usage)

    def _non_retryable(self, err: ProviderError) -> GatewayError:
        if err.kind == "content_policy":
            return GatewayError(
                400, "content_policy_violation", "The provider rejected the request on content policy grounds."
            )
        return GatewayError(502, "upstream_error", f"Provider {err.provider} rejected the request ({err.kind}).")

    async def route(self, adm: Admission) -> tuple[str, CompletionResult]:
        res, tier = self.policy.resilience, adm.req.model
        chain = self.policy.tiers[tier]
        for idx, name in enumerate(chain):
            if not self._configured(name):
                adm.attempts.append(f"{name}: not configured")
                continue
            if name not in adm.output_caps:
                # Became available in a reload after admission: no budget was reserved for it.
                adm.attempts.append(f"{name}: not admitted")
                continue
            breaker = self.breakers[name]
            if not breaker.allow():
                adm.attempts.append(f"{name}: circuit {breaker.state}")
                continue
            for attempt in range(res.max_retries + 1):
                with tracer.start_as_current_span("gateway.llm_call") as span:
                    span.set_attributes({"gateway.provider": name, "gateway.attempt": attempt})
                    t0 = time.perf_counter()
                    try:
                        result = await self._invoke(name, adm.creq, adm.output_caps)
                    except ProviderError as err:
                        elapsed = time.perf_counter() - t0
                        adm.provider_time += elapsed
                        span.set_attribute("gateway.error", err.kind)
                        self._record(name, False, elapsed, err)
                        self.metrics.errors.labels(adm.team.team_id, tier, name, err.kind).inc()
                        adm.attempts.append(f"{name}: {err.kind}")
                        if not err.retryable:
                            breaker.release()
                            adm.failed_cost_usd = err.billed_usd
                            raise self._non_retryable(err) from err
                        breaker.record_failure()
                        if breaker.state != CLOSED or attempt == res.max_retries:
                            break
                        self.metrics.retries.labels(name, err.kind).inc()
                        backoff = self._backoff(attempt)
                        await self._sleep(backoff)
                        adm.provider_time += backoff
                        continue
                    elapsed = time.perf_counter() - t0
                    adm.provider_time += elapsed
                    breaker.record_success()
                    self._record(name, True, elapsed)
                    if idx > 0:
                        self.metrics.fallbacks.labels(tier, chain[0], name).inc()
                    return name, result
        raise GatewayError(
            503,
            "all_providers_unavailable",
            f"No healthy provider for tier {tier!r}; attempts: {adm.attempts}",
            retry_after=res.circuit_cooldown_s,
        )

    # ------------------------------------------------------------------ non-streaming

    async def complete(self, adm: Admission) -> tuple[ChatResponse, dict[str, str]]:
        tier, team = adm.req.model, adm.team
        self.metrics.inflight.inc()
        try:
            try:
                provider, result = await self.route(adm)
            except GatewayError as exc:
                await self.store.settle_budget(team.team_id, adm.reserved_usd, adm.failed_cost_usd)
                self.metrics.requests.labels(team.team_id, tier, "none", str(exc.status_code), adm.req.priority).inc()
                raise
            content = result.content
            if team.enrichment.disclaimer:
                content = f"{content}\n\n{team.enrichment.disclaimer}"
            prompt_t = result.prompt_tokens or sum(estimate_tokens(m["content"]) for m in adm.creq.messages)
            completion_t = result.completion_tokens or estimate_tokens(result.content)
            cost, utilization, headers = await self._settle(adm, provider, prompt_t, completion_t)
            latency = time.perf_counter() - adm.started
            self._observe(adm, provider, latency)
            resp = ChatResponse(
                id=f"chatcmpl-{adm.request_id}",
                created=int(time.time()),
                model=tier,
                choices=[
                    Choice(message=ChatMessage(role="assistant", content=content), finish_reason=result.finish_reason)
                ],
                usage=Usage(
                    prompt_tokens=prompt_t, completion_tokens=completion_t, total_tokens=prompt_t + completion_t
                ),
                x_gateway=GatewayMeta(
                    request_id=adm.request_id,
                    tier=tier,
                    provider=provider,
                    served_model=result.model,
                    fallback_used=provider != self.policy.tiers[tier][0],
                    attempts=adm.attempts,
                    cost_usd=round(cost, 8),
                    latency_ms=round(latency * 1000, 2),
                    budget_utilization=round(utilization, 4),
                ),
            )
            return resp, headers
        finally:
            self.metrics.inflight.dec()

    async def _settle(
        self, adm: Admission, provider: str, prompt_t: int, completion_t: int
    ) -> tuple[float, float, dict]:
        team = adm.team
        cost = self.policy.provider(provider).cost_usd(prompt_t, completion_t)
        with self.metrics.store_duration.time():
            spent = await self.store.settle_budget(team.team_id, adm.reserved_usd, cost)
        utilization = spent / team.daily_budget_usd if team.daily_budget_usd else 1.0
        self.metrics.tokens.labels(team.team_id, provider, "input").inc(prompt_t)
        self.metrics.tokens.labels(team.team_id, provider, "output").inc(completion_t)
        self.metrics.cost.labels(team.team_id, provider).inc(cost)
        self.metrics.budget_spend.labels(team.team_id).set(spent)
        self.metrics.budget_utilization.labels(team.team_id).set(utilization)
        headers = dict(adm.rate_headers)
        headers["X-Request-ID"] = adm.request_id
        headers["X-Gateway-Provider"] = provider
        if utilization >= team.budget_warning_ratio:
            self.metrics.budget_warnings.labels(team.team_id).inc()
            headers["X-Budget-Warning"] = f"{utilization:.0%} of daily budget used"
            prior = (spent - cost) / team.daily_budget_usd if team.daily_budget_usd else 1.0
            if prior < team.budget_warning_ratio:
                log.warning(
                    "team crossed budget warning threshold", extra={"team": team.team_id, "utilization": utilization}
                )
        return cost, utilization, headers

    def _observe(self, adm: Admission, provider: str, latency: float) -> None:
        tier = adm.req.model
        self.metrics.requests.labels(adm.team.team_id, tier, provider, "200", adm.req.priority).inc()
        self.metrics.latency.labels(tier, provider).observe(latency)
        if not adm.req.stream:
            # Streams record overhead at first byte instead (see _sse); total time includes generation.
            self.metrics.overhead.observe(max(0.0, latency - adm.provider_time))
        log.info(
            "request served",
            extra={
                "request_id": adm.request_id,
                "team": adm.team.team_id,
                "tier": tier,
                "provider": provider,
                "attempts": adm.attempts,
                "latency_ms": round(latency * 1000, 2),
                "stream": adm.req.stream,
            },
        )

    # ------------------------------------------------------------------ streaming

    async def _open_stream(
        self, name: str, creq: CompletionRequest, caps: dict[str, int] | None = None
    ) -> tuple[list[StreamChunk], AsyncIterator[StreamChunk], Provider]:
        """Start a provider stream and buffer until the first content delta, so failover stays invisible."""
        fault = self.faults.get(name)
        if fault.latency_ms:
            await asyncio.sleep(fault.latency_ms / 1000)
        if fault.outage:
            raise ProviderError(name, "server_error", "simulated outage", retryable=True, status=503)
        adapter = self.providers[name]
        adapter.acquire()  # released by _sse when the stream ends, or below if opening fails
        try:
            agen = adapter.stream(self._sized(name, creq, caps)).__aiter__()
            buffered: list[StreamChunk] = []
            async for chunk in agen:
                buffered.append(chunk)
                if chunk.delta or chunk.finish_reason:
                    break
        except ProviderError as err:
            self._price_usage(adapter, err)
            await adapter.release()
            raise
        except BaseException:
            await adapter.release()
            raise
        return buffered, agen, adapter

    async def stream(self, adm: Admission) -> tuple[str, AsyncIterator[bytes]]:
        res, tier = self.policy.resilience, adm.req.model
        chain = self.policy.tiers[tier]
        for idx, name in enumerate(chain):
            if not self._configured(name):
                adm.attempts.append(f"{name}: not configured")
                continue
            if name not in adm.output_caps:
                # Became available in a reload after admission: no budget was reserved for it.
                adm.attempts.append(f"{name}: not admitted")
                continue
            breaker = self.breakers[name]
            if not breaker.allow():
                adm.attempts.append(f"{name}: circuit {breaker.state}")
                continue
            for attempt in range(res.max_retries + 1):
                t0 = time.perf_counter()
                try:
                    buffered, agen, adapter = await self._open_stream(name, adm.creq, adm.output_caps)
                except ProviderError as err:
                    elapsed = time.perf_counter() - t0
                    adm.provider_time += elapsed
                    self._record(name, False, elapsed, err)
                    self.metrics.errors.labels(adm.team.team_id, tier, name, err.kind).inc()
                    adm.attempts.append(f"{name}: {err.kind}")
                    if not err.retryable:
                        breaker.release()
                        await self.store.settle_budget(adm.team.team_id, adm.reserved_usd, err.billed_usd)
                        raise self._non_retryable(err) from err
                    breaker.record_failure()
                    if breaker.state != CLOSED or attempt == res.max_retries:
                        break
                    self.metrics.retries.labels(name, err.kind).inc()
                    backoff = self._backoff(attempt)
                    await self._sleep(backoff)
                    adm.provider_time += backoff
                    continue
                adm.provider_time += time.perf_counter() - t0
                if idx > 0:
                    self.metrics.fallbacks.labels(tier, chain[0], name).inc()
                return name, self._sse(adm, name, buffered, agen, t0, adapter)
        await self.store.settle_budget(adm.team.team_id, adm.reserved_usd, 0.0)
        self.metrics.requests.labels(adm.team.team_id, tier, "none", "503", adm.req.priority).inc()
        raise GatewayError(
            503,
            "all_providers_unavailable",
            f"No healthy provider for tier {tier!r}; attempts: {adm.attempts}",
            retry_after=res.circuit_cooldown_s,
        )

    async def _sse(
        self,
        adm: Admission,
        provider: str,
        buffered: list[StreamChunk],
        agen: AsyncIterator[StreamChunk],
        t0: float,
        adapter: Provider,
    ) -> AsyncIterator[bytes]:
        tier, team = adm.req.model, adm.team
        rid = f"chatcmpl-{adm.request_id}"
        parts: list[str] = []
        prompt_t = completion_t = None
        finish = "stop"
        settled = False
        breaker = self.breakers[provider]
        self.metrics.inflight.inc()

        def frame(delta: dict, finish_reason: str | None = None) -> bytes:
            body = {
                "id": rid,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": tier,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            }
            return f"data: {json.dumps(body)}\n\n".encode()

        async def chunks() -> AsyncIterator[StreamChunk]:
            for c in buffered:
                yield c
            async for c in agen:
                yield c

        try:
            first = frame({"role": "assistant", "content": ""})
            # Time-to-first-byte minus provider time-to-first-chunk (and backoff) = gateway overhead.
            self.metrics.overhead.observe(max(0.0, time.perf_counter() - adm.started - adm.provider_time))
            yield first
            async for chunk in chunks():
                if chunk.prompt_tokens is not None:
                    prompt_t = chunk.prompt_tokens
                if chunk.completion_tokens is not None:
                    completion_t = chunk.completion_tokens
                if chunk.finish_reason:
                    finish = chunk.finish_reason
                if chunk.delta:
                    parts.append(chunk.delta)
                    yield frame({"content": chunk.delta})
            if team.enrichment.disclaimer:
                tail = f"\n\n{team.enrichment.disclaimer}"
                parts.append(tail)
                yield frame({"content": tail})
            breaker.record_success()
            self._record(provider, True, time.perf_counter() - t0)
            text = "".join(parts)
            prompt_t = prompt_t or sum(estimate_tokens(m["content"]) for m in adm.creq.messages)
            completion_t = completion_t or estimate_tokens(text)
            cost, utilization, _ = await self._settle(adm, provider, prompt_t, completion_t)
            settled = True
            yield frame({}, finish)
            meta = {
                "request_id": adm.request_id,
                "provider": provider,
                "attempts": adm.attempts,
                "fallback_used": provider != self.policy.tiers[tier][0],
                "cost_usd": round(cost, 8),
                "budget_utilization": round(utilization, 4),
                "usage": {
                    "prompt_tokens": prompt_t,
                    "completion_tokens": completion_t,
                    "total_tokens": prompt_t + completion_t,
                },
            }
            yield f"data: {json.dumps({'x_gateway': meta})}\n\n".encode()
            yield b"data: [DONE]\n\n"
            self._observe(adm, provider, time.perf_counter() - adm.started)
        except ProviderError as err:
            if err.usage:  # the provider reported what it billed (e.g. a refusal): charge exactly that
                self._price_usage(adapter, err)
                await self.store.settle_budget(team.team_id, adm.reserved_usd, err.billed_usd)
                settled = True
            if err.retryable:
                breaker.record_failure()
            else:  # e.g. a refusal: the provider is healthy, so it must not trip the shared breaker
                breaker.release()
            self._record(provider, False, time.perf_counter() - t0, err)
            self.metrics.errors.labels(team.team_id, tier, provider, f"stream_{err.kind}").inc()
            self.metrics.requests.labels(team.team_id, tier, provider, "502", adm.req.priority).inc()
            err_body = {
                "error": {"type": "upstream_stream_error", "message": f"{provider} failed mid-stream ({err.kind})"}
            }
            yield f"data: {json.dumps(err_body)}\n\n".encode()
        finally:
            await adapter.release()
            self.metrics.inflight.dec()
            if not settled:
                # Charge what was delivered (estimate) so aborted streams are not free.
                delivered = estimate_tokens("".join(parts)) if parts else 0
                prompt = prompt_t or sum(estimate_tokens(m["content"]) for m in adm.creq.messages)
                cost = self.policy.provider(provider).cost_usd(prompt, delivered) if parts else 0.0
                await self.store.settle_budget(team.team_id, adm.reserved_usd, cost)

    # ------------------------------------------------------------------ health checks

    async def probe_all(self) -> None:
        names = [n for n, p in self.providers.items() if p.config.probe and p.config.is_configured()]
        await asyncio.gather(*(self.probe(name) for name in names), return_exceptions=True)

    async def probe(self, name: str) -> None:
        if not self._configured(name):
            return
        breaker = self.breakers[name]
        half_open = breaker.current_state() == HALF_OPEN
        if half_open and not breaker.allow():
            return  # a live request is already probing
        t0 = time.perf_counter()
        tracker = self.health[name]
        tracker.last_probe_at = time.time()
        try:
            await self._invoke(name, CompletionRequest(messages=[{"role": "user", "content": "ping"}], max_tokens=1))
        except ProviderError as err:
            self._record(name, False, time.perf_counter() - t0, err)
            if half_open:
                breaker.record_failure()
            return
        except Exception as exc:  # noqa: BLE001 - probes must never crash the loop
            log.warning("probe crashed", extra={"provider": name, "error": repr(exc)})
            if half_open:
                breaker.release()
            return
        self._record(name, True, time.perf_counter() - t0)
        if half_open:
            breaker.record_success()

    def health_report(self) -> dict:
        out = {}
        for name, tracker in self.health.items():
            if name not in self.providers:
                continue
            cfg = self.policy.provider(name)
            status = self._evaluate_health(name)
            fault = self.faults.get(name)
            out[name] = {
                "kind": cfg.kind,
                "model": cfg.model,
                "configured": cfg.is_configured(),
                "status": status if cfg.is_configured() else "not_configured",
                "circuit": self.breakers[name].current_state(),
                **tracker.stats(),
                "last_error": tracker.last_error,
                "last_probe_at": tracker.last_probe_at,
                "simulated_outage": fault.outage,
                "injected_latency_ms": fault.latency_ms,
                "history": [{"ts": ts, "status": s, "reason": r} for ts, s, r in list(tracker.history)[-20:]],
            }
        return out

    async def aclose(self) -> None:
        await asyncio.gather(*(p.aclose() for p in self.providers.values()), return_exceptions=True)
        await self.client.aclose()
        await self.store.close()
