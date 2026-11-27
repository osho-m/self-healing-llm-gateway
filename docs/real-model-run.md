# Real-model verification (local Ollama)

`config/policy.ollama.yml` puts a real local model (`qwen2.5:0.5b` on Ollama 0.40) first in the chain, with a mock behind it as the fallback. Run it on a laptop:

```bash
ollama pull qwen2.5:0.5b
GATEWAY_POLICY_PATH=config/policy.ollama.yml uvicorn app.main:app_factory --factory
```

Observed:

| Step | Result |
| --- | --- |
| Non-streaming request | Real completion from `qwen2.5:0.5b`. Token usage was taken from the provider: 42 prompt + 25 completion tokens. |
| Streaming request | 40 SSE content chunks were relayed. Usage was recorded from the final event. |
| **Ollama process killed** | The first request made 2 attempts; both failed with real `connection` errors ("refused the network connection"). The circuit opened and the mock fallback answered. Later requests skipped the dead provider in about 0.3 ms. |
| Ollama restarted | After the 10 s cooldown, one half-open probe succeeded and the circuit closed. Traffic went back to the real model. |

No cloud API keys were used. The OpenAI and Anthropic adapters are covered by contract tests against recorded response shapes (`tests/test_providers.py`), not by live calls.
