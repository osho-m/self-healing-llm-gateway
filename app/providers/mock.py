"""Deterministic in-process provider for tests, demos and the keyless Space."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from app.providers.base import CompletionRequest, CompletionResult, Provider, StreamChunk, estimate_tokens


def mock_reply(provider: str, messages: list[dict[str, str]], max_tokens: int) -> str:
    last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    text = f"[{provider}] Acknowledged: {last_user[:400]}"
    # Respect max_tokens roughly (4 chars/token).
    return text[: max(4, max_tokens * 4)]


class MockProvider(Provider):
    async def _delay(self) -> None:
        if self.config.mock_latency_ms > 0:
            await asyncio.sleep(self.config.mock_latency_ms / 1000)

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        await self._delay()
        content = mock_reply(self.name, request.messages, request.max_tokens)
        prompt = sum(estimate_tokens(m["content"]) for m in request.messages)
        return CompletionResult(content, prompt, estimate_tokens(content), self.config.model)

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        result = await self.complete(request)
        words = result.content.split(" ")
        for i, word in enumerate(words):
            yield StreamChunk(delta=word if i == 0 else " " + word)
            await asyncio.sleep(0)
        yield StreamChunk(
            prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens, finish_reason="stop"
        )
