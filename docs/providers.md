# Providers

A provider is one model the gateway can call. Each entry under `providers:` in the policy file is one provider, and each tier lists providers by name in the order to try them.

| `kind` | What it talks to | How |
| --- | --- | --- |
| `ollama` | A model served by [Ollama](https://ollama.com) on your machine or server | Ollama's OpenAI-compatible `/v1` API, over aiohttp |
| `openai` | Anything that speaks the OpenAI Chat Completions protocol: vLLM, the bundled mock server, or OpenAI itself | aiohttp |
| `local` | A model running inside the gateway process, such as a Transformers model on a GPU | A direct function call in a worker thread |
| `anthropic` | The Anthropic Messages API | The official `anthropic` Python SDK |
| `mock` | A fake model that always answers the same way, for tests and demos | In process |

The default setups use only open-source models: three Ollama models for local runs and three `local` models in the Hugging Face demo. The `openai` and `anthropic` adapters are there for teams that also use paid hosted APIs.

## Common fields

| Field | Meaning |
| --- | --- |
| `name` | The provider's name, used in tier lists, metrics and logs. |
| `model` | The model ID, for example `qwen2.5:1.5b` on Ollama or `Qwen/Qwen2.5-1.5B-Instruct` for a `local` model. |
| `base_url` | Where the model is served, for proxies and self-hosted servers. |
| `api_key_env` | The name of the environment variable that holds the API key. Keys never go in the policy file. |
| `timeout_s` | How long one call may take. |
| `input_price_per_mtok`, `output_price_per_mtok` | Dollars per million tokens, used for budgets. Leave them at 0 for self-hosted models. |
| `probe` | Whether background health checks call this provider (default `true`). Turn it off for paid APIs: after the cooldown, one real request tests recovery instead. |

## Models running inside the gateway (`local`)

The hosting code loads the model and registers a function for it with `register_local_model(model, fn)`. The gateway itself does not depend on torch or transformers. `demo.py` shows the pattern: it loads three models on a Hugging Face ZeroGPU and registers one function per model.

- A model that is not registered counts as unavailable and is skipped, the same as a provider without an API key.
- Errors while generating (no GPU time left, out of memory, device errors) are treated as temporary: the gateway retries, then moves to the next model.
- A thread cannot be stopped once it starts. So after a timeout the model stays unavailable until that thread finishes, and the gateway sends new requests to the next model instead of piling up work on a stuck one.

## Providers without an API key are skipped

If `api_key_env` is set but the variable is empty, the provider is skipped. It is never called and never trips a circuit. You can see this in four places:

- At startup, a warning names the missing variable.
- `GET /v1/admin/providers` shows `"configured": false` and status `not_configured`.
- Each request lists `"<name>: not configured"` in `x_gateway.attempts`.
- `/readyz` does not count it as serving its tier.

In production mode, the gateway will not start if a tier has no usable provider at all. This lets one policy include optional paid models that switch on when their key is added, for example as a deployment secret.

## Anthropic

The adapter uses the official SDK with its own retries turned off, because the gateway already retries and tracks circuits. Stacking both would multiply attempts and confuse the circuit breaker.

How Anthropic responses map to the gateway:

| From Anthropic | In the gateway |
| --- | --- |
| Text blocks | The answer (other block types, such as thinking, are dropped) |
| Usage | The real token counts |
| `max_tokens` stop reason | Finish reason `length` |
| A refusal | A `content_policy` error (HTTP 400 to the client), never an empty answer. The tokens Anthropic charged for are still counted against the team's budget. |

Rate limits, server errors (including 529 "overloaded"), timeouts, dropped connections and broken JSON are retried. Authentication, permission and bad-request errors are returned right away. Redirects are never followed: a 3xx is rejected as soon as its headers arrive.

`temperature` is not sent, because current Claude models reject it. `effort` sets the reasoning effort. `min_max_tokens` sets a lower limit on `max_tokens`, because current Claude models always think first and thinking tokens count against that limit. The gateway reserves budget for that lower limit when it admits the request.

```yaml
providers:
  - name: claude
    kind: anthropic
    model: claude-opus-5-5
    api_key_env: ANTHROPIC_API_KEY
    input_price_per_mtok: 4.00
    output_price_per_mtok: 20.00
    effort: low
    min_max_tokens: 1024
    probe: false
```

This adapter has not been run against the real Anthropic API in CI, because no key is available. Its tests run over a real network socket against a local fake of the Messages API. They cover request mapping, streaming, error handling, refusals, missing keys and redirects.

## Keeping spend safe on a public demo

- The daily budget is reserved before each call, counted in whole millionths of a dollar so rounding never drifts.
- In the Hugging Face demo, all visitors share one rate-limit and budget counter, so opening a new tab cannot reset it. Outage switches and circuits are separate for each visitor.
- The counter lives in memory, so a restart resets it. For any paid API, also set a spend limit in the provider's console.
