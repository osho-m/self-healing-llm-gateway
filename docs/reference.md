# Reference

Details behind the [README](../README.md).

## How a request flows

```text
client --> auth --> enrichment --> admission --> router --> settle + telemetry
           (team    (system         (one atomic    |
            key)     prompt,         Redis step:   +-- retry with backoff (temporary errors only)
                     blocklist)      rate limit    +-- circuit breaker per model
                                     + budget)     +-- fallback to the next model in the tier
                                                         |
                                  Ollama / vLLM / in-process model / hosted API / mock

/metrics --> Prometheus --> Grafana (Operations, Business, Performance)
                       \--> Alertmanager --> Slack          OTLP traces --> Jaeger
```

| Part | Where |
| --- | --- |
| Request pipeline, routing, streaming | `app/gateway.py` |
| Model adapters and error handling | `app/providers/` |
| Rate limits, budgets, runtime overrides (Redis and in-memory) | `app/stores.py` |
| Circuit breaker, health tracking, fault injection | `app/resilience.py` |
| Policy file, validation, hot reload | `app/policy.py` |
| HTTP API and admin endpoints | `app/api.py` |

## API

| Endpoint | Auth | Purpose |
| --- | --- | --- |
| `POST /v1/chat/completions` | team | Chat completion; streams when `stream: true` |
| `GET /v1/models` | team | The team's tiers and their fallback order |
| `GET /healthz`, `GET /readyz` | none | Liveness and readiness |
| `GET /metrics` | none | Prometheus metrics (restrict at the network layer) |
| `GET /v1/admin/providers` | admin | Model health, circuit state, error rate, latency, history |
| `POST /v1/admin/providers/{name}/simulate-outage` | admin | Turn a fake outage on or off |
| `GET /v1/admin/teams/{id}/status` | admin | Limits, what is left, spend and budget use |
| `PATCH /v1/admin/teams/{id}` | admin | Change limits, budget or tiers at runtime (audited) |
| `GET /v1/admin/audit` | admin | Recent admin actions |

Errors use the OpenAI shape `{"error": {"type", "code", "message"}}`:

| Status | Code | Meaning |
| --- | --- | --- |
| `400` | `content_policy_violation` | Blocked by the team's rules or by the model |
| `401`, `403` | `invalid_api_key`, `tier_not_allowed` | Wrong key, or the team may not use this tier |
| `429` | `rate_limit_exceeded`, `budget_exceeded` | Limit or daily budget reached (`Retry-After` is set) |
| `502` | `upstream_error` | The model rejected the request |
| `503` | `all_providers_unavailable` | Every model in the tier is down |

## Configuration

One policy file lists the models, the fallback order for each tier, the teams and the retry settings. It is checked when loaded and reloaded when it changes. If a new version is invalid, the gateway keeps the last good one. Changes made through `PATCH /v1/admin/teams/{id}` win over the file and are shared through Redis.

```yaml
providers:
  - { name: smollm2, kind: ollama, model: "smollm2:1.7b", base_url: "http://localhost:11434/v1" }
  - { name: qwen,    kind: ollama, model: "qwen2.5:1.5b", base_url: "http://localhost:11434/v1" }
  - { name: phi,     kind: ollama, model: "phi3.5:3.8b",  base_url: "http://localhost:11434/v1" }
tiers:
  economy:  [smollm2, qwen, phi]
  balanced: [qwen, phi, smollm2]
  premium:  [phi, qwen, smollm2]
```

Hosted APIs are added the same way, with `api_key_env` naming the environment variable that holds the key and per-token prices for budget tracking. See [docs/providers.md](docs/providers.md).

Store team keys only as SHA-256 hashes (`python scripts/hash_key.py`). Production mode will not start if Redis is missing, if the admin key is short or a known placeholder, or if the policy contains a plain-text key. See [SECURITY.md](SECURITY.md).

## Monitoring

The gateway reports requests, errors, retries and fallbacks; latency split into total time, model time, gateway time and Redis time; tokens and cost; circuit state and model health; rate-limit rejections; and budget use per team.

Three Grafana dashboards (Operations, Business, Performance) load from `deploy/grafana/dashboards`. Alert rules in `deploy/prometheus/alerts.yml` fire on a high model error rate, an open circuit, a tier with no healthy model, budget thresholds, the latency target, and gateway overhead.

## Tests and CI

```bash
pip install -e '.[dev]'
ruff check . && ruff format --check . && mypy
pytest -q --cov=app            # set REDIS_URL to include the Redis tests
```

Every pull request runs lint and type checks, the tests on Python 3.11 to 3.13 against a real Redis (Redis tests fail instead of skipping when Redis is missing), a check of the Prometheus and Alertmanager configs, a Docker test that boots the whole stack and causes real HTTP failures, and two load tests that fail the build if gateway overhead gets worse.

## Roadmap

1. A long-term usage ledger for invoicing (Postgres or ClickHouse). Redis keeps 48 hours.
2. Circuit state shared across replicas through Redis.
3. Single sign-on for admins instead of a shared admin key, and audit logs shipped to storage that cannot be edited.
4. Idempotency keys, and one deadline per request across all its retries.
5. A target for time to first token when streaming.
6. Real tokenizers for the admission estimate, instead of assuming about 4 characters per token.
