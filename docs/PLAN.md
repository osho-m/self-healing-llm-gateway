# Build plan: production LLM gateway

## Goal

Make the repository deliver what its README described. The starting point was a Gradio-only demo (`demo.py` and `space_gateway.py`), while the README referenced a FastAPI `app/`, tests, Docker Compose, k6 and Grafana, none of which existed.

The specification is *Project 11: LLM Gateway with Rate Limiting, Fallback Routing, and Observability*. Done means two things:

- every command in the README works;
- every phase of the specification is either built and tested, or listed explicitly in the backlog.

## Acceptance criteria → where it lives

| # | Spec requirement | Implementation | Verified by |
| --- | --- | --- | --- |
| 1.1 | Unified provider interface (OpenAI, Anthropic, Ollama) | `app/providers/*` | `tests/test_providers.py` |
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
| 5.2 | Load test with 5,000+ concurrent mixed requests; overhead under 10 ms | `loadtests/loadtest.py` (closed-loop capacity + open-loop `--rate`), `loadtests/gateway.js` | CI `compose-smoke`: capacity 296–463 req/s. At 100 req/s, overhead p50 was 1.7–3.8 ms in every run, while p95 (3.9–12.1 ms) and p99 met 10 ms in only some runs; 25 ms p95 regression guard. **10 ms SLO not yet verified on dedicated hardware.** See README |
| 5.3 | Compose: gateway, Redis, Prometheus, Grafana, mock providers, demo teams | `docker-compose.yml`, `config/policy.compose.yml` | CI `compose-smoke` |
| 6 | Demo script and narrative | `scripts/demo.ps1`, `scripts/demo.sh`, README | manual |

## Phases (thin, tested slices)

1. Scaffolding: `pyproject.toml`, settings, policy model with hot reload and overrides.
2. Providers and error classification; mock provider HTTP service.
3. Stores: Redis Lua token buckets and budget ledger, with an in-memory equivalent.
4. Router: retries, circuit breakers, fallback chains; streaming.
5. Admin API, audit log, health monitor; metrics, traces, logs.
6. Ops: Dockerfile, compose, Prometheus, Alertmanager, Grafana, Jaeger; demo scripts; load tests.
7. CI: lint, tests against Redis, config validation, compose smoke test and load test.
8. Docs: README reflects what exists; the backlog lists what does not.

## Key decisions

- **Tier, not vendor, in `model`.** Fallback chains are defined per tier, so callers never depend on a specific vendor.
- **Reserve then settle for budgets.** This prevents concurrent and streaming requests from overspending; a check made only after the call cannot.
- **Gateway-side fault injection and mock-side faults.** The first gives demos deterministic outages. The second (`POST /fault` on the mock provider) exercises the real HTTP error path in CI.
- **Breaker state stays per replica.** It is cheap and fast. If replicas must agree immediately, sharing it through Redis is listed in the backlog.
- **The Space reuses the production engine.** `demo.py` imports `app.gateway`, so the demo cannot drift from the shipped code.
