# Build plan

## Goal

The repository started as a Gradio demo (`demo.py` and `space_gateway.py`), while its README described a FastAPI service with tests, Docker Compose, k6 and Grafana that did not exist. The goal was to build what the README promised.

The specification is *Project 11: LLM Gateway with Rate Limiting, Fallback Routing, and Observability*. The work counts as done when:

- every command in the README works;
- every part of the specification is either built and tested, or listed in the roadmap.

## Each requirement, where it is built and how it is tested

| # | Spec requirement | Implementation | Verified by |
| --- | --- | --- | --- |
| 1.1 | Unified provider interface (Ollama, OpenAI-compatible servers such as vLLM, in-process models, Anthropic) | `app/providers/*` | `tests/test_providers.py` |
| 1.2 | Team API keys, allowed models, routing | `app/api.py` `require_team`, `app/policy.py` | `test_api.py::test_tier_restrictions`, `test_requires_valid_key` |
| 1.3 | Streaming passthrough with full-response logging | `Gateway.stream/_sse` | `test_streaming_*` |
| 1.4 | Per-team enrichment (system prompt, disclaimer, filter) | `Gateway._enrich`, disclaimer in `complete/_sse` | `test_enrichment_*` |
| 2.1 | Atomic, distributed token buckets (RPM/TPM), 429 + Retry-After | `app/stores.py` (`_ACQUIRE_LUA`) | `test_stores.py` (memory + Redis in CI) |
| 2.2 | Budget caps, 80% warning, clear block error | reserve/settle ledger | `test_budget_*`, `test_concurrent_budget_reservations_never_exceed_cap` |
| 2.3 | Priority tiers: realtime served near capacity | `batch_reserve_ratio` floor in the bucket script | `test_batch_cannot_drain_realtime_reserve` |
| 2.4 | Admin API, runtime changes, audit (who/when) | `app/api.py` admin router, `app/audit.py` | `test_admin_*` |
| 3.1 | Health checks, rolling error rate and P99, health history | `HealthTracker`, `Gateway.probe_all` loop | `test_health_tracker_statuses`, `test_health_probe_closes_half_open_circuit` |
| 3.2 | Fallback chains per tier | `policy.tiers`, `Gateway.route` | `test_outage_triggers_retry_then_fallback_and_circuit` |
| 3.3 | Retry with exponential backoff; only retryable errors | `Gateway.route`, `error_from_status` | `test_retryable_http_errors_*`, `test_non_retryable_error_does_not_fall_back` |
| 3.4 | Circuit breaker (N in M s, half-open single probe, log transitions) | `app/resilience.py` | `tests/test_resilience.py` |
| 4.1 | OpenTelemetry spans with team, model, tokens, latency, cost | `app/observability.py`, spans in `api.py`/`gateway.py` | Jaeger in compose |
| 4.2 | Prometheus metrics | `Metrics` | `test_metrics_and_health_endpoints` |
| 4.3 | Three Grafana dashboards | `scripts/gen_dashboards.py` → `deploy/grafana/dashboards` | CI regenerates and diffs |
| 4.4 | Alert rules routed to Slack | `deploy/prometheus/alerts.yml`, `deploy/alertmanager` | CI `promtool` / `amtool` |
| 5.1 | Integration tests: limits under concurrency, budgets, fallback, breakers, streaming | `tests/` | CI, Python 3.11–3.13 with Redis |
| 5.2 | Load test with 5,000+ mixed requests (spec says "concurrent"); overhead under 10 ms | `loadtests/loadtest.py` (closed-loop capacity + open-loop `--rate`), `loadtests/gateway.js` | CI `compose-smoke` on a 4-vCPU runner with real Redis. Capacity run: 5,000 mixed requests, at most 500 in flight, up to 630 req/s. Overhead run: 3,000 requests at a fixed 100 req/s. A true 5,000-in-flight test was not run; overhead at 100 req/s p50/p95/p99 1.6–1.8 / 2.4–3.0 / 2.5–4.7 ms over two runs, so **under 10 ms at p99**. A 25 ms p95 regression guard stays in place for noisy runners. See README |
| 5.3 | Compose: gateway, Redis, Prometheus, Grafana, mock providers, demo teams | `docker-compose.yml`, `config/policy.compose.yml` | CI `compose-smoke` |
| 6 | Demo script and narrative | `scripts/demo.ps1`, `scripts/demo.sh`, README | manual |

## Order of work

Each step was small and tested before the next one started.

1. Project setup: `pyproject.toml`, settings, and the policy file with hot reload and runtime overrides.
2. Model adapters and error handling, plus a mock model server.
3. Counters: Redis Lua scripts for rate limits and budgets, with an in-memory version for development.
4. Router: retries, circuit breakers, fallback order and streaming.
5. Admin API, audit log, health checks, metrics, traces and logs.
6. Operations: Dockerfile, Docker Compose, Prometheus, Alertmanager, Grafana, Jaeger, demo scripts and load tests.
7. CI: lint, tests against Redis, config checks, a Docker test and a load test.
8. Docs: the README describes only what exists; the roadmap lists the rest.

## Main decisions

- **Apps ask for a tier, not a model.** Each tier has its own fallback order, so apps never depend on one vendor.
- **Budgets are reserved first and settled after.** Checking only after a call cannot stop parallel or streaming requests from overspending.
- **Two kinds of fake outage.** The gateway can fake an outage for demos, and the mock model server can return real HTTP errors (`POST /fault`) so CI tests the real error path.
- **Gateway copies share circuit opens through Redis.** When one copy opens a model's circuit, it writes a Redis key that expires with the cooldown, and the other copies open the same circuit within about a second. Only opens are shared: each copy still counts its own failures, so the request path never waits on Redis for this.
- **The demo runs the real gateway code.** `demo.py` imports `app.gateway`, so the demo can never drift from what ships.
- **Open-source models by default.** Every shipped setup runs SmolLM2, Qwen2.5 and Phi-3.5. Paid APIs are optional and turn on only when their key is set.
