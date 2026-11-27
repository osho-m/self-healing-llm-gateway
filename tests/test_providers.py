import json

import httpx
import pytest

from app.policy import ProviderConfig
from app.providers import CompletionRequest, ProviderError, build_provider

REQ = CompletionRequest(
    messages=[{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}], max_tokens=16
)


def provider(kind, handler, **cfg):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return build_provider(ProviderConfig(name=f"{kind}-p", kind=kind, model="m", **cfg), client)


async def test_openai_translation_and_auth_header(monkeypatch):
    monkeypatch.setenv("TEST_OPENAI_KEY", "sk-test")
    seen = {}

    def handler(request: httpx.Request):
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "gpt-x",
                "choices": [{"message": {"content": "yo"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 1},
            },
        )

    result = await provider("openai", handler, api_key_env="TEST_OPENAI_KEY").complete(REQ)
    assert (result.content, result.prompt_tokens, result.completion_tokens, result.model) == ("yo", 7, 1, "gpt-x")
    assert seen["auth"] == "Bearer sk-test"
    assert seen["body"]["messages"][0]["role"] == "system" and seen["body"]["max_tokens"] == 16


@pytest.mark.parametrize(
    ("status", "body", "kind", "retryable"),
    [
        (429, "slow down", "rate_limited", True),
        (503, "overloaded", "server_error", True),
        (401, "bad key", "auth", False),
        (400, '{"error":{"code":"content_policy_violation"}}', "content_policy", False),
        (400, "bad request", "bad_request", False),
    ],
)
async def test_error_classification(status, body, kind, retryable):
    p = provider("openai", lambda r: httpx.Response(status, text=body))
    with pytest.raises(ProviderError) as exc:
        await p.complete(REQ)
    assert exc.value.kind == kind and exc.value.retryable is retryable


async def test_timeout_is_retryable():
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(ProviderError) as exc:
        await provider("ollama", handler).complete(REQ)
    assert exc.value.kind == "timeout" and exc.value.retryable


async def test_openai_streaming_parses_sse_and_usage():
    chunks = [
        {"choices": [{"delta": {"content": "Hel"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
    ]
    sse = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    p = provider("openai", lambda r: httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"}))
    out = [c async for c in p.stream(REQ)]
    assert "".join(c.delta for c in out) == "Hello"
    assert out[-1].prompt_tokens == 3 and out[-1].completion_tokens == 2
