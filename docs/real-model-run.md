# Running against a real model (local Ollama)

This run used one real model on a laptop: `qwen2.5:0.5b` on Ollama 0.40, with a mock as the fallback. The commands were:

```bash
ollama pull qwen2.5:0.5b
GATEWAY_POLICY_PATH=config/policy.ollama.yml uvicorn app.main:app_factory --factory
```

What happened:

| Step | Result |
| --- | --- |
| Normal request | The model answered. Token counts came from Ollama itself: 42 prompt and 25 completion tokens. |
| Streaming request | 40 chunks of text were passed through to the client. Token counts came from the last chunk. |
| **Ollama process killed** | The request was tried twice, and both tries failed with real "connection refused" errors. The circuit opened and the mock answered. Later requests skipped the dead model in about 0.3 ms. |
| Ollama restarted | After the 10 second cooldown, one test request went through and the circuit closed. Traffic went back to the real model. |

`config/policy.ollama.yml` has since moved to three open-source models (SmolLM2-1.7B, Qwen2.5-1.5B and Phi-3.5-mini). Each tier now falls back to the other two models.

No paid API keys were used. The OpenAI-compatible and Anthropic adapters are tested against recorded responses and a local fake API, not against the live services.
