---
title: Self-Healing LLM Gateway
emoji: 🛡️
colorFrom: blue
colorTo: purple
sdk: gradio
app_file: demo.py
python_version: "3.12"
pinned: false
short_description: No-key LLM gateway with automatic failover.
tags:
  - llm
  - fastapi
  - observability
  - reliability
---

# Self-Healing LLM Gateway

[![CI](https://github.com/osho-m/self-healing-llm-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/osho-m/self-healing-llm-gateway/actions/workflows/ci.yml)

An OpenAI-compatible API gateway that sits in front of every LLM call in an organization. It:

- authenticates teams and restricts them to capability tiers;
- enforces per-team **requests/min and tokens/min** with atomic Redis token buckets, keeping headroom for realtime traffic over batch traffic;
- enforces **daily dollar budgets**, warning at 80% and blocking at 100%;
- **retries** retryable errors with exponential backoff, opens **circuit breakers** on failing providers, and **fails over** along a per-tier fallback chain;
- streams responses (SSE) and accounts for them in full;
- exports **Prometheus metrics**, **OpenTelemetry traces** and structured JSON logs, with three Grafana dashboards and Slack alerting.

The stack runs end to end without any cloud credentials. Deterministic mock providers make outage and failover behaviour easy to demonstrate. Real OpenAI, Anthropic and Ollama adapters are included: switching to them is a policy change, not a code change.

## Architecture

```text
             ┌──────────────────────────── gateway (FastAPI) ────────────────────────────┐
client ──▶   auth ─▶ enrichment ─▶ rate limit ─▶ budget reserve ─▶ router ─▶ settle/telemetry
 (OpenAI     (team    (system       (Redis Lua     (Redis Lua         │
  format)     key)     prompt,       token          micro-USD         ├─ retry + backoff (retryable only)
                       blocklist)    buckets)       ledger)           ├─ circuit breaker per provider
                                                                      └─ tier fallback chain
                                                                            │
                                         ┌──────────────┬───────────────────┼──────────────┐
                                     OpenAI API    Anthropic API        Ollama       mock providers
   /metrics ─▶ Prometheus ─▶ Grafana (Operations / Business / Performance)
                        └──▶ Alertmanager ─▶ Slack          OTLP traces ─▶ Jaeger
```

| Concern | Implementation |
| --- | --- |
| Provider abstraction | `app/providers/`: one normalized request/response; adapters for OpenAI-compatible APIs, Anthropic Messages, Ollama, and in-process mocks. Errors are classified as retryable (429, 5xx, timeouts, connection) or not (auth, bad request, content policy). |
| Policy | `config/*.yml`, validated with Pydantic, **hot-reloaded** on change. Runtime overrides from the admin API take precedence and are shared through Redis (see [policy precedence](#policy-precedence)). |
| Rate limiting | Token buckets for requests and tokens per team, in a single atomic Lua script that uses Redis server time. Batch requests must leave `batch_reserve_ratio` of each bucket for realtime requests. Returns `429` with `Retry-After`. |
| Budgets | Before each call the worst-case cost along the tier chain is **reserved**, then settled at actual cost. Concurrent and streaming requests therefore cannot overshoot the cap. Integer micro-dollars; resets at 00:00 UTC. |
| Resilience | Up to `max_retries` with jittered exponential backoff. A breaker opens after N failures in M seconds, and after the cooldown lets **exactly one** half-open probe through. Background health checks probe every provider, which also heals half-open circuits without user traffic. Non-retryable errors are returned immediately and never trigger fallback. |
| Streaming | Failover happens before the first content byte, so clients never see a switch. After that, a mid-stream failure ends the stream with an error event. Usage and cost are recorded from the assembled stream. Aborted streams are charged for what was delivered. |
| Enrichment | Per-team system prompt injection, response disclaimer, and blocked-term filter. |
| Admin | Separate admin key (or an `admin`-role team key). Every change is written to an append-only audit log recording who changed what, and when. |
| Observability | See [metrics](#metrics-and-alerts). Spans: request, auth, rate limit, budget, route, and one LLM call per attempt, carrying team, tier, provider, served model, tokens, cost and latency. |

## Quick start

### Full stack (Docker)

```bash
cp .env.example .env      # optional: admin key, Slack webhook, Grafana password, real provider keys
docker compose up --build
```

| Service | URL |
| --- | --- |
| Gateway / OpenAPI docs | http://localhost:8000/docs |
| Grafana (3 dashboards, anonymous view; admin from `.env`) | http://localhost:3000 |
| Prometheus / alerts | http://localhost:9090 |
| Alertmanager | http://localhost:9093 |
| Jaeger traces | http://localhost:16686 |
| Mock providers (fault injection via `POST /fault`) | http://localhost:9001, http://localhost:9002 |

Then run the guided demo, which covers failover, circuit recovery, rate limiting, budgets and audited admin changes:

```powershell
.\scripts\demo.ps1          # or: ./scripts/demo.sh
```

The compose policy, `config/policy.compose.yml`, sets up demo teams with different limits: `demo`, `search` (30 req/min), `compliance` (with a disclaimer), `batch` (30% realtime reserve) and `loadtest`. Edit the file while the stack is running and the gateway hot-reloads it.

### Local (no Docker)

```bash
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\Activate.ps1
pip install -e '.[dev]'
GATEWAY_ADMIN_API_KEY=demo-admin-key-change-me-0000 uvicorn app.main:app_factory --factory --reload
```

Without `GATEWAY_REDIS_URL` the gateway uses in-process stores, which is fine for a single replica in development. `GATEWAY_ENVIRONMENT=production` refuses to start without Redis, with a short or placeholder admin key, or with plaintext `api_keys` in the policy (see [SECURITY.md](SECURITY.md)).

### Hugging Face Space (no Docker, no keys)

`demo.py` runs **the same gateway engine** in-process with mock providers. Hugging Face installs `requirements.txt` and starts it automatically. In the UI you can send requests, toggle a `mock-primary` outage, watch traffic fail over, and run a health probe to see the circuit close.

Live at [huggingface.co/spaces/oshom/self-healing-llm-gateway](https://huggingface.co/spaces/oshom/self-healing-llm-gateway). The Space runs on either CPU basic or ZeroGPU hardware. ZeroGPU will not start a Space unless it defines a `@spaces.GPU` function, so `demo.py` defines a no-op one whenever the `spaces` package is installed. The gateway itself never uses a GPU.

## API

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' -H 'X-API-Key: demo-live-key' \
  -d '{"model":"balanced","messages":[{"role":"user","content":"Explain circuit breakers."}]}'
```

`model` is a **capability tier** (`economy`, `balanced`, `premium`), not a vendor model. Optional fields: `stream`, `max_tokens`, `temperature`, and `priority` (`realtime` | `batch`). Responses are OpenAI-shaped and add an `x_gateway` block: provider, served model, fallback flag, attempts, cost, latency and budget utilization. Headers: `X-Request-ID`, `X-Gateway-Provider`, `X-RateLimit-Remaining-*`, and `X-Budget-Warning` once a team passes its warning threshold.

| Endpoint | Auth | Purpose |
| --- | --- | --- |
| `POST /v1/chat/completions` | team | Chat completion, SSE when `stream: true` |
| `GET /v1/models` | team | Tiers available to the team and their fallback chains |
| `GET /healthz`, `GET /readyz` | none | Liveness / readiness (store reachable, every tier has a non-open provider) |
| `GET /metrics` | none (restrict at the network layer) | Prometheus |
| `GET /v1/admin/providers` | admin | Health status, circuit, error rate, P50/P99, health history |
| `POST /v1/admin/providers/{name}/simulate-outage?enabled=` | admin | Chaos: gateway-side outage |
| `POST /v1/admin/providers/{name}/inject-latency?ms=` | admin | Chaos: added latency |
| `POST /v1/admin/providers/{name}/probe` | admin | Run a health probe now |
| `GET /v1/admin/teams`, `GET /v1/admin/teams/{id}/status` | admin | Limits, remaining buckets, spend, utilization, alert state |
| `PATCH /v1/admin/teams/{id}` | admin | Change limits, budget or tiers at runtime (audited) |
| `DELETE /v1/admin/teams/{id}/overrides` | admin | Revert to the policy file |
| `GET /v1/admin/audit` | admin | Recent admin actions |
| `POST /v1/admin/policy/reload` | admin | Force a policy reload |

Admin authentication uses `X-Admin-Key: $GATEWAY_ADMIN_API_KEY`, or the `X-API-Key` of a team whose policy `role` is `admin`. Standard team keys get `403`.

Errors use the OpenAI shape `{"error": {"type", "code", "message"}}`:

| Status | Code | Meaning |
| --- | --- | --- |
| 400 | `content_policy_violation` | The team blocklist or the provider rejected the content |
| 401 / 403 | `invalid_api_key`, `tier_not_allowed`, `forbidden` | Authentication or authorization failed |
| 429 | `rate_limit_exceeded` | Request or token bucket is empty (`Retry-After` set) |
| 429 | `budget_exceeded` | Daily cap reached (`Retry-After` is the time until 00:00 UTC) |
| 502 | `upstream_error` | Non-retryable provider error, such as a bad provider credential |
| 503 | `all_providers_unavailable` | The tier's fallback chain is exhausted |

## Policy precedence

1. Runtime overrides (`PATCH /v1/admin/teams/{id}`), stored in Redis so every replica sees them.
2. The YAML policy file, re-read when its mtime changes. An invalid file is rejected and the last good policy stays active.

Overrides survive hot reloads. After you copy a change into the YAML file, clear the override with `DELETE …/overrides` so the file stays the long-term source of truth. Store only `api_key_sha256` hashes in real policies (`python scripts/hash_key.py`). Plaintext `api_keys` exist for local demos only.

### Using real providers

Add a provider entry and reference it from a tier chain; keys are read from the environment variable named by `api_key_env`:

```yaml
providers:
  - { name: openai-gpt4o, kind: openai, model: gpt-4o, api_key_env: OPENAI_API_KEY,
      input_price_per_mtok: 2.50, output_price_per_mtok: 10.00 }
  - { name: anthropic-sonnet, kind: anthropic, model: claude-sonnet-5-5, api_key_env: ANTHROPIC_API_KEY,
      input_price_per_mtok: 3.00, output_price_per_mtok: 15.00 }
  - { name: ollama-local, kind: ollama, model: "llama3.2:3b", base_url: "http://host.docker.internal:11434/v1" }
tiers:
  premium: [openai-gpt4o, anthropic-sonnet, ollama-local]
```

Prices are deployment configuration. Reconcile tracked spend against provider invoices.

## Metrics and alerts

| Metric | Labels |
| --- | --- |
| `gateway_requests_total` | team, tier, provider, status, priority |
| `gateway_errors_total` | team, tier, provider, error_type |
| `gateway_request_duration_seconds` / `gateway_provider_duration_seconds` | tier/provider |
| `gateway_overhead_seconds` | time in the gateway excluding provider calls and backoff |
| `gateway_tokens_total`, `gateway_cost_usd_total` | team, provider, direction |
| `gateway_fallbacks_total`, `gateway_retries_total` | tier, from/to provider; provider, error_type |
| `gateway_circuit_state` (0/1/2), `gateway_circuit_state_changes_total`, `gateway_provider_health` | provider |
| `gateway_rate_limited_total` | team, limit (`requests`/`tokens`/`budget`), priority |
| `gateway_budget_utilization_ratio`, `gateway_budget_spend_usd`, `gateway_budget_warnings_total` | team |

Grafana dashboards are provisioned from `deploy/grafana/dashboards` and generated by `scripts/gen_dashboards.py`:

- **Operations**: circuit state, provider health, error rates, fallbacks, retries, limit rejections.
- **Business**: budget utilization, spend, cost rate per team and provider, usage trends.
- **Performance**: gateway overhead percentiles, latency P50/P95/P99 per provider, token throughput.

Alert rules (`deploy/prometheus/alerts.yml`) route to Slack through Alertmanager. Set `SLACK_WEBHOOK_URL` to enable delivery. The rules are:

- provider error rate above 5%
- circuit open
- a tier with no healthy provider
- a team at 80% or 100% of its budget
- P99 latency above the 5 s SLA
- gateway P99 overhead above 10 ms (sustained for 10 minutes)

## Verification

```bash
pip install -e '.[dev]'
ruff check . && ruff format --check .
pytest -q --cov=app                     # set REDIS_URL to also run the Redis store tests
```

The test suite covers:

- rate limits that hold exactly under concurrency, including the batch reserve;
- budget caps that block at the threshold;
- retry, fallback and no-fallback on non-retryable errors;
- circuit open, half-open and close, including recovery through a health probe;
- streaming output that matches non-streaming output, plus failover before the first byte;
- enrichment, admin RBAC and auditing, and policy hot reload.

CI runs the suite on Python 3.11–3.13 against a real Redis instance; the Redis tests fail rather than skip if Redis is missing. CI also validates the Prometheus and Alertmanager configs, boots the compose stack, smoke-tests failover against real HTTP faults injected into the mock provider, and runs two load tests: a 5,000-request capacity run, and an open-loop run at 100 req/s that fails CI if overhead p95 exceeds 25 ms. That 25 ms limit is a regression guard sized for noisy shared runners.

## Load test

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

**CI.** Every PR runs the `compose-smoke` job on a GitHub-hosted runner. The full compose stack (Redis, both mocks, Jaeger) and the load generator share one small VM. The latest run on this branch:

| Mode | Throughput | Overhead p50 / p95 / p99 | Store calls p50 / p95 / p99 | Outcome |
| --- | --- | --- | --- | --- |
| Capacity: closed loop, 500 in flight, 5,000 requests | **463 req/s** | 73 / 182 / 237 ms (queueing) | 35 / 83 / 159 ms | 4,413 × 200, 587 × 429, zero errors |
| SLO: open loop at 100 req/s, 3,000 requests | 100 req/s | **1.7 / 3.9 / 7.7 ms** | 0.27 / 0.86 / 3.3 ms | 2,615 × 200, 385 × 429, zero errors |

On identical code, overhead p95 at 100 req/s ranged from **3.9 to 12.1 ms** across runs, and capacity from 296 to 463 req/s. That spread depends on which runner the job lands on: in the slowest run, Redis calls alone had p95 4.3 ms, against 0.86 ms in the fastest. CI therefore uses this run as a **regression guard**, failing if p95 exceeds 25 ms, and does not treat it as proof of the 10 ms target.

p99 is reported but not gated, because it is noisy on shared runners: 7.7, 21.4 and 21.8 ms across the last three runs on identical code. Redis calls at p99 (3.3 ms) do not account for the tail; it is CPU scheduling on a 2-vCPU VM that also runs Redis, both mocks, Jaeger and the load generator.

**The <10 ms target is met at p50 in every CI run, at p95 and p99 in some runs, and at every percentile locally.** It has not been verified on dedicated hardware with Redis. That measurement is the remaining step before quoting it as an SLO. All values are interpolated within histogram buckets.

**Local.** Windows 11 laptop with the in-memory store, so there are no Redis round trips:

| Mode | Throughput | Overhead p50 / p95 / p99 |
| --- | --- | --- |
| Capacity, 300 in flight | 570 req/s | 0.28 / 0.80 / 0.98 ms |
| Open loop at 200 req/s | 200 req/s | 0.72 / 1.73 / 2.35 ms |
| Capacity, 500 in flight, primary outage halfway through | 388 req/s | 0.72 / 2.13 / 2.49 ms; 2,287 requests served by the fallback, zero 5xx |

**How throughput went from 83 to 463 req/s in CI:**

1. **httpx was the bottleneck.** Against the same mock provider, httpx fell from 204 to 89 req/s as concurrency rose from 10 to 200, while aiohttp reached 3,346 req/s. The gateway now calls providers through aiohttp (`app/http.py`).
2. **Admission is one Redis round trip.** Rate limiting and budget reservation run in a single Lua script.
3. **Lighter middleware.** A pure ASGI middleware replaced Starlette's `BaseHTTPMiddleware`.
4. **Trace sampling.** Head-based sampling uses `GATEWAY_OTEL_SAMPLE_RATIO`, set to 0.1 in compose. Span export holds the GIL in a background thread.

`gc.freeze()` also runs after startup.

To scale further, run more gateway replicas: Redis already holds all shared limit and budget state.

## Production notes and backlog

Done: Redis-backed atomic limits and ledger, provider adapters with timeouts, admin RBAC and an audit log, OpenTelemetry traces, alert rules, load tests, CI.

Still open:

1. Durable usage ledger for invoicing (Postgres or ClickHouse). The Redis ledger keeps 48 hours.
2. Circuit-breaker and health state live in each replica. Share breaker state through Redis if replicas must agree immediately.
3. Admin SSO/OIDC in place of a shared admin key; audit log shipping to immutable storage.
4. Idempotency keys and per-request deadline budgets across retries.
5. `LatencyP99AboveSLA` uses end-to-end duration, which includes long streamed generations; add a time-to-first-token SLO for streams.
6. Response caching tier, and token counting with provider tokenizers instead of the 4-chars-per-token admission estimate.

See [docs/PLAN.md](docs/PLAN.md) for the build plan and [SECURITY.md](SECURITY.md) for the security model.
