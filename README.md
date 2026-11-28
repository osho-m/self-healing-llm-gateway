# Self-Healing LLM Gateway

[![CI](https://github.com/osho-m/self-healing-llm-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/osho-m/self-healing-llm-gateway/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-3776AB)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Live demo](https://img.shields.io/badge/live%20demo-Hugging%20Face-orange)](https://huggingface.co/spaces/oshom/self-healing-llm-gateway)

A gateway that sits between your apps and your AI models. It keeps each team within its limits and budget, and when a model fails, it switches to another one so your app keeps answering.

**[Try the live demo](https://huggingface.co/spaces/oshom/self-healing-llm-gateway)**

![Live demo: Qwen answers; then Qwen is switched off and the gateway fails over to Phi-3.5-mini](docs/images/failover-demo.gif)

## The problem

When several teams share AI models, three things go wrong: one busy team uses up capacity another needs, a runaway job spends the month's budget in an afternoon, and one model going down takes the product with it. The gateway handles all three in one place.

## How it works

Every request takes the same path:

1. **Check the team.** The gateway reads the team's API key and adds the team's rules, such as a system prompt.
2. **Admit or refuse.** In one atomic step in Redis, it checks the team's rate limit and reserves the most the request could cost. A team over its limit or budget gets a clear `429` answer.
3. **Pick a healthy model.** Apps ask for a tier (`economy`, `balanced` or `premium`), not a model. The gateway tries the tier's models in order, retries short failures, and skips any model whose circuit breaker is open.
4. **Charge the real cost.** After the model answers, the gateway charges what the call really cost and releases the rest of the reservation.

A circuit breaker opens when a model keeps failing, so traffic moves to the next model. After a cooldown, one test request checks whether the model has recovered. When several copies of the gateway run behind a load balancer, they share open circuits through Redis.

## What it does

- **Limits per team:** requests and tokens per minute, with batch jobs kept from starving live users.
- **Budgets in dollars:** reserve first, settle after, so parallel and streaming requests cannot overspend.
- **Self-healing:** retries with backoff, circuit breakers, and fallback across three open-source models (SmolLM2, Qwen2.5 and Phi-3.5).
- **Any model:** Ollama, vLLM, models running on a GPU inside the gateway, or hosted APIs such as Anthropic.
- **Easy to watch:** Prometheus metrics, OpenTelemetry traces, Grafana dashboards and Slack alerts.
- **Safe by default:** production mode refuses unsafe settings, keys are stored only as hashes, and every admin change is audited.

## Results

| What | Result |
| --- | --- |
| Time the gateway adds to a request | 2.5 to 4.7 ms (p99, at 100 requests per second, with Redis) |
| Most requests handled per second | 630, with zero errors |
| Speed-up from one fix | 83 to 463 requests per second, after replacing a slow HTTP client |
| Real model outage | The gateway switched to the backup model and switched back when the model returned |
| Live demo | Qwen2.5-1.5B answered in 3.3 s. With Qwen down, Phi-3.5-mini answered in 4.4 s |
| Tests | 112 tests on Python 3.11 to 3.13, run on every change |

How these were measured: [benchmarks](docs/benchmarks.md).

## See the healing

In the [live demo](https://huggingface.co/spaces/oshom/self-healing-llm-gateway), send a request, then tick the model your tier uses under "Simulate an outage of" and send again. The gateway retries, then another model answers.

| A model answers | The model is down, so another model answers |
| --- | --- |
| ![Normal request](docs/images/space-normal.png) | ![Failover to another model](docs/images/space-failover.png) |

The demo runs three open-source models on a free Hugging Face GPU. When your free GPU time runs out, the gateway falls back to a simple built-in answer and the health panel says why.

## Tech stack

Python, FastAPI, aiohttp, Redis (Lua scripts), Hugging Face Transformers, Ollama, Docker, Prometheus, Grafana, OpenTelemetry, Jaeger, GitHub Actions, Gradio.

## Learn more

- [Getting started](docs/getting-started.md): run it with Docker or on your own machine
- [Reference](docs/reference.md): API, errors, configuration, monitoring and roadmap
- [Models](docs/providers.md): every supported model type and its settings
- [Benchmarks](docs/benchmarks.md): how the speed numbers were measured
- [Real-model run](docs/real-model-run.md): a real outage on a laptop
- [Build plan](docs/PLAN.md): each requirement, where it is built and how it is tested
- [Security](SECURITY.md)

## License

[MIT](LICENSE)
