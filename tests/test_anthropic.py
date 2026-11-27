"""AnthropicProvider (official SDK) against a local fake of the Messages API, over a real socket."""

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.policy import ProviderConfig
from app.providers import CompletionRequest, ProviderError, build_provider
from app.providers.anthropic import AnthropicProvider

REQ = CompletionRequest(
    messages=[{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}],
    max_tokens=16,
    temperature=0.7,
)


def message(text="hello", stop="end_turn", **extra):
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": text}] if text is not None else [],
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {"input_tokens": 9, "output_tokens": 2},
        **extra,
    }


def sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


STREAM = [
    {
        "type": "message_start",
        "message": {**message(text=None, stop=None), "usage": {"input_tokens": 5, "output_tokens": 1}},
    },
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi"}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": " there"}},
    {"type": "content_block_stop", "index": 0},
    {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": 2},
    },
    {"type": "message_stop"},
]


class FakeAnthropic:
    def __init__(self):
        self.mode = "ok"
        self.requests: list[dict] = []
        self.stolen: list[str | None] = []
        self.redirect_to = ""

    async def messages(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        self.requests.append({"headers": dict(request.headers), "body": body})
        if self.mode == "429":
            return web.json_response(
                {"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}}, status=429
            )
        if self.mode == "401":
            return web.json_response(
                {"type": "error", "error": {"type": "authentication_error", "message": "bad key"}}, status=401
            )
        if self.mode == "529":
            return web.json_response(
                {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}, status=529
            )
        if self.mode == "redirect":
            raise web.HTTPTemporaryRedirect(location=self.redirect_to)
        if self.mode == "redirect_stall":
            # Headers arrive, the body never does.
            resp = web.StreamResponse(status=307, headers={"Location": self.redirect_to, "Content-Length": "100"})
            await resp.prepare(request)
            await asyncio.sleep(30)
            return resp
        if self.mode == "bad_usage":
            return web.json_response(message(usage={"input_tokens": "lots", "output_tokens": 2}))
        if self.mode == "bad_json":
            if body.get("stream"):
                return web.Response(text="event: message_start\ndata: {not json\n\n", content_type="text/event-stream")
            return web.Response(text="{not json", content_type="application/json")
        if self.mode == "refusal":
            return web.json_response(
                message(
                    text=None, stop="refusal", stop_details={"type": "refusal", "category": "cyber", "explanation": "x"}
                )
            )
        if self.mode == "stream_overloaded" and body.get("stream"):
            events = [STREAM[0], {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}]
            return web.Response(text=sse(events), content_type="text/event-stream")
        if self.mode == "stream_drop" and body.get("stream"):
            resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            resp.enable_chunked_encoding()
            await resp.prepare(request)
            await resp.write(sse(STREAM[:3]).encode())
            request.transport.close()  # connection dies mid-stream
            return resp
        if body.get("stream"):
            return web.Response(text=sse(STREAM), content_type="text/event-stream")
        return web.json_response(message())

    async def stolen_endpoint(self, request: web.Request) -> web.Response:
        self.stolen.append(request.headers.get("Authorization") or request.headers.get("x-api-key"))
        return web.json_response(message())


@pytest.fixture
async def fake(monkeypatch):
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "sk-test-secret")
    api = FakeAnthropic()
    app = web.Application()
    app.router.add_post("/v1/messages", api.messages)
    app.router.add_post("/stolen", api.stolen_endpoint)
    server = TestServer(app)
    await server.start_server()
    api.base_url = str(server.make_url(""))
    yield api
    await server.close()


def make(api, **cfg) -> AnthropicProvider:
    config = ProviderConfig(
        name="claude",
        kind="anthropic",
        model="claude-opus-5-5",
        base_url=api.base_url,
        api_key_env="TEST_ANTHROPIC_KEY",
        timeout_s=5,
        **cfg,
    )
    provider = build_provider(config, client=None)  # the SDK owns its HTTP client
    assert isinstance(provider, AnthropicProvider)
    return provider


async def test_complete_maps_request_and_response(fake):
    p = make(fake, effort="low")
    try:
        result = await p.complete(REQ)
    finally:
        await p.aclose()
    assert (result.content, result.prompt_tokens, result.completion_tokens) == ("hello", 9, 2)
    assert result.model == "claude-opus-5-5" and result.finish_reason == "stop"
    sent = fake.requests[0]
    assert sent["headers"]["Authorization"] == "Bearer sk-test-secret"
    body = sent["body"]
    assert body["system"] == "be brief"
    assert [m["role"] for m in body["messages"]] == ["user"]
    assert body["max_tokens"] == 16  # sized by the gateway (floor capped at the admission reservation)
    assert body["output_config"] == {"effort": "low"}
    assert "temperature" not in body  # current Claude models reject sampling params


async def test_streaming_yields_text_and_usage(fake):
    p = make(fake)
    try:
        chunks = [c async for c in p.stream(REQ)]
    finally:
        await p.aclose()
    assert "".join(c.delta for c in chunks) == "Hi there"
    assert chunks[0].prompt_tokens == 5
    assert chunks[-1].completion_tokens == 2 and chunks[-1].finish_reason == "stop"


@pytest.mark.parametrize(
    ("mode", "kind", "retryable"),
    [("429", "rate_limited", True), ("529", "server_error", True), ("401", "auth", False)],
)
async def test_errors_are_classified_without_sdk_retries(fake, mode, kind, retryable):
    fake.mode = mode
    p = make(fake)
    try:
        with pytest.raises(ProviderError) as exc:
            await p.complete(REQ)
    finally:
        await p.aclose()
    assert exc.value.kind == kind and exc.value.retryable is retryable
    assert len(fake.requests) == 1  # max_retries=0: the gateway owns retries


async def test_refusal_is_a_non_retryable_content_policy_error(fake):
    fake.mode = "refusal"
    p = make(fake)
    try:
        with pytest.raises(ProviderError) as exc:
            await p.complete(REQ)
    finally:
        await p.aclose()
    assert exc.value.kind == "content_policy" and not exc.value.retryable
    assert "cyber" in str(exc.value)
    assert exc.value.usage == (9, 2)  # billed usage is carried for budget settlement


async def test_cross_origin_redirect_never_receives_the_api_key(fake):
    """A provider 3xx to another origin must not carry the API key there."""
    other = FakeAnthropic()
    app = web.Application()
    app.router.add_post("/stolen", other.stolen_endpoint)
    attacker = TestServer(app)
    await attacker.start_server()
    fake.mode = "redirect"
    fake.redirect_to = str(attacker.make_url("/stolen"))
    p = make(fake)
    try:
        with pytest.raises(ProviderError) as exc:
            await p.complete(REQ)
    finally:
        await p.aclose()
        await attacker.close()
    assert exc.value.kind == "unexpected_redirect" and not exc.value.retryable
    assert other.stolen == []  # redirects are not followed: neither key nor prompt leaves the origin


async def test_connection_refused_is_retryable(monkeypatch):
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "k")
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    config = ProviderConfig(
        name="c",
        kind="anthropic",
        model="m",
        base_url=f"http://127.0.0.1:{port}",
        timeout_s=2,
        api_key_env="TEST_ANTHROPIC_KEY",
    )
    p = build_provider(config, client=None)
    try:
        with pytest.raises(ProviderError) as exc:
            await p.complete(REQ)
    finally:
        await p.aclose()
    assert exc.value.kind in ("connection", "timeout") and exc.value.retryable


async def test_missing_api_key_is_a_clean_non_retryable_error(monkeypatch):
    monkeypatch.delenv("NOPE_KEY", raising=False)
    config = ProviderConfig(name="c", kind="anthropic", model="m", api_key_env="NOPE_KEY")
    p = build_provider(config, client=None)
    with pytest.raises(ProviderError) as exc:
        await p.complete(REQ)
    assert exc.value.kind == "auth" and not exc.value.retryable


@pytest.mark.parametrize(("mode", "kind"), [("stream_overloaded", "server_error"), ("stream_drop", "connection")])
async def test_mid_stream_failures_are_retryable_provider_errors(fake, mode, kind):
    fake.mode = mode
    p = make(fake)
    try:
        with pytest.raises(ProviderError) as exc:
            [c async for c in p.stream(REQ)]
    finally:
        await p.aclose()
    assert exc.value.retryable and exc.value.kind in (kind, "timeout")


async def test_redirect_is_rejected_on_headers_even_if_its_body_stalls(fake):
    fake.mode = "redirect_stall"
    fake.redirect_to = "http://example.invalid/stolen"
    p = make(fake)
    try:
        with pytest.raises(ProviderError) as exc:
            await asyncio.wait_for(p.complete(REQ), timeout=3)  # adapter timeout is 5 s
    finally:
        await p.aclose()
    assert exc.value.kind == "unexpected_redirect" and not exc.value.retryable


@pytest.mark.parametrize("streaming", [False, True])
async def test_malformed_json_is_a_retryable_bad_response(fake, streaming):
    fake.mode = "bad_json"
    p = make(fake)
    try:
        with pytest.raises(ProviderError) as exc:
            if streaming:
                [c async for c in p.stream(REQ)]
            else:
                await p.complete(REQ)
    finally:
        await p.aclose()
    assert exc.value.kind == "bad_response" and exc.value.retryable


async def test_invalid_usage_count_is_a_retryable_bad_response(fake):
    fake.mode = "bad_usage"
    p = make(fake)
    try:
        with pytest.raises(ProviderError) as exc:
            await p.complete(REQ)
    finally:
        await p.aclose()
    assert exc.value.kind == "bad_response" and exc.value.retryable
