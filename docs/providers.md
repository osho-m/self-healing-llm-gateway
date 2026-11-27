# Providers

Each entry under `providers:` in the policy file is one upstream model. Tiers reference providers by name, in fallback order.

| `kind` | Adapter | Transport |
| --- | --- | --- |
| `openai` | OpenAI-compatible Chat Completions (OpenAI, vLLM, the bundled mock service) | aiohttp |
| `ollama` | Ollama's OpenAI-compatible `/v1` API | aiohttp |
| `anthropic` | Anthropic Messages API | official `anthropic` Python SDK, aiohttp backend |
| `mock` | Deterministic in-process provider for tests and demos | none |

## Common fields

| Field | Meaning |
| --- | --- |
| `name` | Unique name used in tier chains, metrics and logs. |
| `model` | The upstream model ID, e.g. `claude-opus-5-5`. |
| `base_url` | Override the provider endpoint (proxies, self-hosted, tests). |
| `api_key_env` | Name of the environment variable that holds the key. Keys are never stored in the policy. |
| `timeout_s` | Per-call timeout. |
| `input_price_per_mtok`, `output_price_per_mtok` | USD per million tokens, used for budget reservation and spend accounting. Set from the provider's current price list. |
| `probe` | Whether background health probes call this provider (default `true`). Set `false` for paid providers. An open circuit then recovers through a single half-open live request after the cooldown, instead of a probe request every interval. |

## Unconfigured providers are skipped, not called

If `api_key_env` is set but the variable is empty, the provider is **skipped**: it is never called and never trips a circuit. This shows up in four places:

- Startup logs a warning naming the missing variable.
- `GET /v1/admin/providers` reports `"configured": false` and status `not_configured`.
- Each skipped request lists `"<name>: not configured"` in `x_gateway.attempts`.
- `/readyz` does not count an unconfigured provider as serving its tier.

In production mode, startup fails if any tier has no configured provider at all.

This lets one policy describe optional paid tiers. They switch on when the key is provided, for example as a deployment secret.

## Anthropic

The adapter uses the official SDK with `max_retries=0`, because the gateway owns retries, backoff and circuit breaking. SDK retries stacked inside the gateway's would multiply attempts and skew the breaker.

**Mapping:**

| From Anthropic | Becomes in the gateway |
| --- | --- |
| Text content blocks | the response content (other block types, such as thinking, are dropped) |
| Usage | the provider's real token counts |
| `max_tokens` stop reason | finish reason `length` |
| Refusal (`stop_reason: "refusal"`) | non-retryable `content_policy` error (HTTP 400 to the client), never an empty success |

**Errors:**
- **Retried:** 429, 5xx (including 529 "overloaded"), timeouts and connection errors.
- **Not retried:** authentication, permission and bad-request errors are returned immediately.

**Request parameters:**
- **Sampling parameters** such as `temperature` are not forwarded, because current Claude models reject them.
- **`effort`** (`low` … `max`) sets the reasoning effort.
- **`min_max_tokens`** sets a floor for `max_tokens`. Current Claude models always think, and thinking tokens count against `max_tokens`, so a very small cap can leave no room for the answer.

**Model fallback** is decided by the gateway's tier chain, not by Anthropic's server-side fallbacks. That keeps spend accounting per provider and the routing policy in one place.

Example:

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

## Spend safety for public deployments

- The team's daily budget is reserved before every call, with integer micro-dollar accounting.
- In the Hugging Face demo, all visitors share one rate-limit and budget store, so the cap is process-wide. Opening new tabs cannot reset it. Circuits and fault switches stay per visitor.
- The store is in memory, so a restart resets it. Set a spend limit at the provider as well (for Anthropic, in the Console) as the hard backstop.

The Anthropic adapter's live path has not been exercised against the real API in this repository's CI, because no key is available. It is tested over a real socket against a local fake of the Messages API: request mapping, streaming, error classification, refusals, missing keys, and redirects to another origin.
