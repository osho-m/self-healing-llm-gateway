"""kind: local - in-process model provider and its gateway behaviour."""

import time

import pytest

from app.policy import ProviderConfig
from app.providers import CompletionRequest, ProviderError, build_provider
from app.providers.local import LocalGeneration, register_local_model, unregister_local_model
from tests.conftest import admin, chat, policy_dict

REQ = CompletionRequest(messages=[{"role": "user", "content": "hi"}], max_tokens=8)


@pytest.fixture
def model():
    name = "test/local-model"
    calls = []

    def generate(messages, max_tokens):
        calls.append((messages, max_tokens))
        return LocalGeneration(text="local answer here", prompt_tokens=7, completion_tokens=3)

    register_local_model(name, generate)
    yield name, calls
    unregister_local_model(name)


def provider(name, **cfg):
    return build_provider(ProviderConfig(name="local", kind="local", model=name, **cfg), client=None)


async def test_complete_and_stream_use_registered_model(model):
    name, calls = model
    p = provider(name)
    assert p.is_available()
    result = await p.complete(REQ)
    assert (result.content, result.prompt_tokens, result.completion_tokens) == ("local answer here", 7, 3)
    assert calls[0] == (REQ.messages, 8)
    chunks = [c async for c in p.stream(REQ)]
    assert "".join(c.delta for c in chunks) == "local answer here" and chunks[-1].completion_tokens == 3


async def test_runtime_failures_are_retryable():
    def boom(messages, max_tokens):
        raise RuntimeError("ZeroGPU quota exceeded")

    register_local_model("test/boom", boom)
    try:
        with pytest.raises(ProviderError) as exc:
            await provider("test/boom").complete(REQ)
    finally:
        unregister_local_model("test/boom")
    assert exc.value.kind == "local_runtime" and exc.value.retryable


async def test_slow_generation_times_out_retryable():
    def slow(messages, max_tokens):
        time.sleep(0.5)
        return LocalGeneration("late", 1, 1)

    register_local_model("test/slow", slow)
    try:
        with pytest.raises(ProviderError) as exc:
            await provider("test/slow", timeout_s=0.05).complete(REQ)
    finally:
        unregister_local_model("test/slow")
    assert exc.value.kind == "timeout" and exc.value.retryable


def test_unregistered_local_model_is_skipped_like_a_missing_key(make_client):
    data = policy_dict()
    data["providers"].insert(0, {"name": "gpu", "kind": "local", "model": "test/not-loaded"})
    data["tiers"]["balanced"] = ["gpu", "primary"]
    client = make_client(data)
    meta = chat(client).json()["x_gateway"]
    assert meta["provider"] == "primary" and meta["attempts"] == ["gpu: not configured"]
    assert admin(client, "GET", "/v1/admin/providers").json()["gpu"]["status"] == "not_configured"


def test_registered_local_model_serves_and_fails_over(make_client, model):
    name, _ = model
    data = policy_dict()
    data["providers"].insert(0, {"name": "gpu", "kind": "local", "model": name})
    data["tiers"]["balanced"] = ["gpu", "primary"]
    client = make_client(data)
    r = chat(client)
    assert r.json()["x_gateway"]["provider"] == "gpu"
    assert r.json()["choices"][0]["message"]["content"] == "local answer here"
    admin(client, "POST", "/v1/admin/providers/gpu/simulate-outage", params={"enabled": "true"})
    assert chat(client).json()["x_gateway"]["provider"] == "primary"
