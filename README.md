---
title: Self-Healing LLM Gateway
emoji: 🛡️
colorFrom: blue
colorTo: purple
sdk: gradio
app_file: demo.py
pinned: false
short_description: Live LLM gateway demo with failover, circuit breakers, and budgets.
tags:
  - llm
  - fastapi
  - observability
  - reliability
---

# Self-Healing LLM Gateway

A production-oriented FastAPI gateway for LLM workloads. It authenticates teams, enforces request/token limits and daily spend caps, selects a model tier, retries transient errors, opens circuits for unhealthy providers, and fails over automatically. Every decision is exposed through Prometheus metrics.

The repository deliberately ships with deterministic mock providers. It runs end to end without cloud credentials and makes outage/failover behavior easy to demonstrate. The included Gradio interface is ready for a no-Docker Hugging Face Space. Replace the `MockProvider` adapter with OpenAI, Anthropic, or Ollama adapters for a real deployment; the gateway contract and resilience policy remain unchanged.

## Architecture

```text
Client → API-key auth → rate/budget policy → tier routing → circuit breaker → provider
                                      │                         │
                                      └──── Prometheus metrics ◀─┘
                                                        │
                                                     Grafana
```

Fallbacks are configured by capability tier, not vendor model name:

| Tier | Primary | Fallback |
| --- | --- | --- |
| economy | mock-primary | mock-secondary |
| balanced | mock-primary | mock-secondary |
| premium | mock-primary | mock-secondary |

## Run locally

```bash
python -m venv .venv
. .venv/bin/activate  # Windows: .venv\\Scripts\\Activate.ps1
pip install -e '.[dev]'
uvicorn app.main:app --reload
```

Or bring up the operational stack:

```bash
docker compose up --build
```

Endpoints: gateway `http://localhost:8000/docs`, Prometheus `:9090`, Grafana `:3000` (`admin` / `admin`).

## Hugging Face Space (no Docker)

Create a **Gradio** Space named `self-healing-llm-gateway`, then push this repository to it. Hugging Face installs [requirements.txt](requirements.txt) and starts [demo.py](demo.py) automatically. The Space is a public, no-key demo: it uses deterministic local mock providers and does not expose a real provider credential.

The live interface lets reviewers send a request, simulate a `mock-primary` outage, see automatic fallback to `mock-secondary`, and inspect the circuit state. Keep the Docker files for the full observability stack; they are not required for the Space.

### Optional keyless local model

The default mock mode is the recommended portfolio demo because provider failures are deterministic. To use a real local model without an API key, install Ollama, run `ollama pull llama3.2:3b`, then set `GATEWAY_PROVIDER_MODE=ollama` and `OLLAMA_MODEL=llama3.2:3b` before launching the gateway. The gateway calls Ollama at `host.docker.internal:11434` when containerized; set `OLLAMA_BASE_URL` if it differs. A second Ollama host is needed to demonstrate genuine cross-host failover.

## Demo request

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' -H 'X-API-Key: demo-live-key' \
  -d '{"model":"balanced","messages":[{"role":"user","content":"Explain circuit breakers."}]}'
```

Trigger recovery behavior without touching infrastructure:

```bash
curl -X POST 'http://localhost:8000/v1/admin/providers/mock-primary/simulate-outage?enabled=true' \
  -H 'X-API-Key: demo-live-key'
```

The next `balanced` request retries the primary, opens its circuit after repeated errors, and completes through `mock-secondary`. Restore it with `enabled=false`; after the cooldown it will be tried in half-open state.

For a complete no-key walkthrough, with the stack running:

```powershell
.\scripts\demo.ps1
```

## API and operational behavior

- `POST /v1/chat/completions` accepts a familiar chat-style request and supports SSE when `stream: true`.
- `GET /v1/models` lists tiers available to the authenticated team.
- `GET /v1/admin/teams/{team_id}/status` returns spend and budget utilization.
- `PATCH /v1/admin/teams/{team_id}` changes limits and budgets at runtime. Production deployments should protect admin routes with an admin role, not a standard team key.
- `GET /metrics` exports request, token, cost, fallback, latency, and circuit state telemetry.

The current `GatewayStore` is an in-process reference implementation, intentionally simple for demos/tests. Docker Compose includes Redis so the next deployment step is isolated: replace this adapter with Redis Lua scripts for atomic token-bucket checks and a durable spend ledger. Treat model pricing as deployment configuration and reconcile spend against provider invoices. `config/policy.example.yml` documents the operational policy shape.

## Verification

```bash
pip install -e '.[dev]'
ruff check .
pytest -q
```

## Load test

With the gateway running, execute the included k6 workload:

```bash
k6 run loadtests/gateway.js
```

It exercises a mixed real-time/batch traffic profile and accepts `200` and intentionally limited `429` responses. Capture the k6 output and Grafana dashboard screenshot for the portfolio case study; report measured results only.

## Production hardening backlog

1. Redis-backed rate limits and a durable usage ledger (Postgres/ClickHouse).
2. Provider-specific adapters with credential rotation, idempotency keys, and timeout budgets.
3. Separate admin authentication/RBAC, audit persistence, encrypted secrets, and tenant isolation.
4. OpenTelemetry traces, alert rules, load tests, and SLOs before handling production traffic.
