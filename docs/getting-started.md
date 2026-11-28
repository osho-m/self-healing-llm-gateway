# Getting started

How to run the gateway yourself. To try it without installing anything, use the [live demo](https://huggingface.co/spaces/oshom/self-healing-llm-gateway).

## With Docker

This starts the gateway, Redis, two mock models, Prometheus, Alertmanager, Grafana and Jaeger:

```bash
cp .env.example .env
docker compose up --build
```

When it is up, open these on the same machine:

| Service | Address |
| --- | --- |
| Gateway API docs | `localhost:8000/docs` |
| Grafana dashboards | `localhost:3000` |
| Prometheus | `localhost:9090` |
| Jaeger traces | `localhost:16686` |

A guided script then shows failover, circuit recovery, rate limits, budgets and audited admin changes:

```bash
./scripts/demo.sh        # Windows: .\scripts\demo.ps1
```

## On your own machine, with three open-source models

Install [Ollama](https://ollama.com) and pull the three models:

```bash
ollama pull smollm2:1.7b
ollama pull qwen2.5:1.5b
ollama pull phi3.5:3.8b
```

Install the gateway and start it:

```bash
pip install -e '.[dev]'
GATEWAY_POLICY_PATH=config/policy.ollama.yml GATEWAY_ADMIN_API_KEY=local-admin-key-0123456789 \
  uvicorn app.main:app_factory --factory
```

On Windows PowerShell:

```powershell
pip install -e '.[dev]'
$env:GATEWAY_POLICY_PATH = "config/policy.ollama.yml"
$env:GATEWAY_ADMIN_API_KEY = "local-admin-key-0123456789"
uvicorn app.main:app_factory --factory
```

Without `GATEWAY_REDIS_URL` the gateway keeps its counters in memory. That is fine for one copy in development; production mode requires Redis.

## Send a request

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' -H 'X-API-Key: demo-live-key' \
  -d '{"model":"balanced","messages":[{"role":"user","content":"Explain circuit breakers."}]}'
```

The answer has the same shape as an OpenAI chat completion, plus an `x_gateway` section that shows which model answered, every attempt, the cost and how much of the budget is used.

## Run the tests

```bash
pip install -e '.[dev]'
ruff check . && ruff format --check . && mypy
pytest -q --cov=app            # set REDIS_URL to include the Redis tests
```

Next: the [reference](reference.md) covers the API, errors, configuration and monitoring.
