"""Standalone OpenAI-compatible mock provider with fault injection.

Used by docker-compose and load tests so the gateway talks to providers over real HTTP:
retries, timeouts and connection pooling behave exactly as with a hosted vendor.

    uvicorn mock_provider.server:app --port 9001

Fault controls (no auth; never expose publicly):
    POST /fault {"error_rate": 0.5, "status": 503, "latency_ms": 200}
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

NAME = os.environ.get("MOCK_PROVIDER_NAME", "mock")
BASE_LATENCY_MS = float(os.environ.get("MOCK_LATENCY_MS", "20"))

app = FastAPI(title=f"Mock LLM provider ({NAME})")


class Fault(BaseModel):
    error_rate: float = Field(default=0.0, ge=0, le=1)
    status: int = Field(default=503, ge=400, le=599)
    latency_ms: float = Field(default=0.0, ge=0)


state = Fault()


def _tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


@app.get("/health")
async def health():
    return {"status": "ok", "provider": NAME, "fault": state.model_dump()}


@app.post("/fault")
async def set_fault(fault: Fault):
    global state
    state = fault
    return state.model_dump()


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    await asyncio.sleep((BASE_LATENCY_MS + state.latency_ms) / 1000)
    if state.error_rate and random.random() < state.error_rate:
        return JSONResponse(
            {"error": {"message": f"{NAME} injected failure", "type": "server_error"}}, status_code=state.status
        )
    messages = body.get("messages", [])
    last = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
    text = f"[{NAME}] Acknowledged: {last[:400]}"[: max(4, int(body.get("max_tokens", 512)) * 4)]
    prompt_t = sum(_tokens(m.get("content", "")) for m in messages)
    usage = {"prompt_tokens": prompt_t, "completion_tokens": _tokens(text), "total_tokens": prompt_t + _tokens(text)}
    rid, created, model = f"chatcmpl-{uuid.uuid4().hex}", int(time.time()), body.get("model", NAME)

    if not body.get("stream"):
        return {
            "id": rid,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": usage,
        }

    async def events():
        for i, word in enumerate(text.split(" ")):
            chunk = {
                "id": rid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {"content": word if i == 0 else " " + word}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(chunk)}\n\n"
            await asyncio.sleep(0.002)
        final = {
            "id": rid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": usage,
        }
        yield f"data: {json.dumps(final)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")
