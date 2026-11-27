"""AiohttpClient against a real local HTTP server, driven through the OpenAI adapter."""

import asyncio
import json
import socket

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.http import AiohttpClient
from app.policy import ProviderConfig
from app.providers import CompletionRequest, ProviderError, build_provider

REQ = CompletionRequest(messages=[{"role": "user", "content": "hi"}], max_tokens=8)


async def chat(request: web.Request) -> web.StreamResponse:
    mode = request.headers.get("x-mode", "ok")
    body = await request.json()
    if mode == "503":
        return web.json_response({"error": {"message": "busy"}}, status=503)
    if mode == "slow":
        await asyncio.sleep(1)
    if body.get("stream"):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        for part in ("Hel", "lo"):
            chunk = {"choices": [{"delta": {"content": part}, "finish_reason": None}]}
            await resp.write(f"data: {json.dumps(chunk)}\r\n\r\n".encode())
        await resp.write(b'data: {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}}\n\n')
        await resp.write(b"data: [DONE]\n\n")
        return resp
    return web.json_response(
        {
            "model": "m",
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
    )


@pytest.fixture
async def server():
    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    srv = TestServer(app)
    await srv.start_server()
    yield srv
    await srv.close()


def provider(base_url: str, client: AiohttpClient, mode: str = "ok", timeout_s: float = 5.0):
    cfg = ProviderConfig(name="p", kind="openai", model="m", base_url=base_url, timeout_s=timeout_s)
    p = build_provider(cfg, client)
    p._headers = lambda: {"x-mode": mode}  # route the fake server's behaviour
    return p


async def test_complete_and_stream_over_real_socket(server):
    client = AiohttpClient()
    try:
        p = provider(str(server.make_url("/v1")), client)
        result = await p.complete(REQ)
        assert result.content == "ok" and result.prompt_tokens == 1
        chunks = [c async for c in p.stream(REQ)]
        assert "".join(c.delta for c in chunks) == "Hello"
        assert chunks[-1].completion_tokens == 2
    finally:
        await client.aclose()


async def test_status_errors_and_timeouts_are_classified(server):
    client = AiohttpClient()
    try:
        with pytest.raises(ProviderError) as exc:
            await provider(str(server.make_url("/v1")), client, mode="503").complete(REQ)
        assert exc.value.kind == "server_error" and exc.value.retryable
        with pytest.raises(ProviderError) as exc:
            await provider(str(server.make_url("/v1")), client, mode="slow", timeout_s=0.1).complete(REQ)
        assert exc.value.kind == "timeout" and exc.value.retryable
    finally:
        await client.aclose()


async def test_connection_refused_is_retryable():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    client = AiohttpClient()
    try:
        with pytest.raises(ProviderError) as exc:
            await provider(f"http://127.0.0.1:{port}/v1", client).complete(REQ)
        assert exc.value.kind == "connection" and exc.value.retryable
    finally:
        await client.aclose()
