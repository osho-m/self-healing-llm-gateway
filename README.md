# Self-Healing LLM Gateway

[![CI](https://github.com/osho-m/self-healing-llm-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/osho-m/self-healing-llm-gateway/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-3776AB)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Live demo](https://img.shields.io/badge/live%20demo-Hugging%20Face-orange)](https://huggingface.co/spaces/oshom/self-healing-llm-gateway)

A gateway that sits between your apps and your AI models. It keeps each team within its limits, and when a model fails, it switches to another one so your app keeps answering.

**[Try the live demo](https://huggingface.co/spaces/oshom/self-healing-llm-gateway)**

![Live demo: Qwen answers; then Qwen is switched off and the gateway fails over to Phi-3.5-mini](docs/images/failover-demo.gif)

## What it does

- **Limits per team.** Caps requests and tokens per minute for each team.
- **Budgets in dollars.** Reserves the cost before a call and charges the real cost after, so no team can overspend.
- **Heals itself.** Retries failed calls, stops sending traffic to a broken model, and switches to the next one. It checks later whether the broken model has recovered.
- **Works with any model.** Runs three open-source models by default (SmolLM2, Qwen2.5 and Phi-3.5). Paid APIs like Anthropic or OpenAI can be added in the config.
- **Easy to watch.** Comes with metrics, traces, Grafana dashboards and Slack alerts.

## Results

| What | Result |
| --- | --- |
| Time the gateway adds to a request | 2.5 to 4.7 ms (p99, at 100 requests per second, with Redis) |
| Most requests handled per second | 630, with zero errors |
| Speed-up from one fix | 83 to 463 requests per second, after replacing a slow HTTP client |
| Real model outage | The gateway switched to the backup model and switched back when the model returned |
| Live demo | Qwen2.5-1.5B answered in 3.3 s. With Qwen down, the gateway switched and Phi-3.5-mini answered in 4.4 s |
| Tests | 104 tests, run on every change |

How these were measured: [docs/benchmarks.md](docs/benchmarks.md).

## See the healing

In the [live demo](https://huggingface.co/spaces/oshom/self-healing-llm-gateway), send a request, then tick the model your tier uses under "Simulate an outage of" and send again. The gateway retries, then another model answers.

| A model answers | The model is down, so another model answers |
| --- | --- |
| ![Normal request](docs/images/space-normal.png) | ![Failover to another model](docs/images/space-failover.png) |

The demo runs three open-source models on a free Hugging Face GPU. When your free GPU time runs out, the gateway falls back to a simple built-in answer and the health panel says why.

## Run it

With Docker (gateway, Redis, Grafana, Prometheus and Jaeger):

```bash
cp .env.example .env
docker compose up --build
```

Open http://localhost:8000/docs for the API, and http://localhost:3000 for Grafana.

Or on your own machine with three open-source models from [Ollama](https://ollama.com):

```bash
ollama pull smollm2:1.7b && ollama pull qwen2.5:1.5b && ollama pull phi3.5:3.8b
pip install -e '.[dev]'
GATEWAY_POLICY_PATH=config/policy.ollama.yml GATEWAY_ADMIN_API_KEY=local-admin-key-0123456789 \
  uvicorn app.main:app_factory --factory
```

On Windows PowerShell:

```powershell
$env:GATEWAY_POLICY_PATH = "config/policy.ollama.yml"
$env:GATEWAY_ADMIN_API_KEY = "local-admin-key-0123456789"
uvicorn app.main:app_factory --factory
```

Send a request:

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' -H 'X-API-Key: demo-live-key' \
  -d '{"model":"balanced","messages":[{"role":"user","content":"Explain circuit breakers."}]}'
```

The answer looks like an OpenAI chat completion, plus a `x_gateway` section that shows which model answered, every attempt, and the cost.

## Learn more

- [Reference](docs/reference.md): API, errors, configuration, monitoring, tests and roadmap
- [Models](docs/providers.md): every supported model type and its settings
- [Benchmarks](docs/benchmarks.md): how the speed numbers were measured
- [Real-model run](docs/real-model-run.md): a real outage on a laptop
- [Build plan](docs/PLAN.md): each requirement, where it is built and how it is tested
- [Security](SECURITY.md)

## License

[MIT](LICENSE)
