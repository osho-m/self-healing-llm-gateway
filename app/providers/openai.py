"""OpenAI-compatible adapter (OpenAI, Ollama's /v1 API, the bundled mock provider service)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from app.providers.base import (
    CompletionRequest,
    CompletionResult,
    Provider,
    ProviderError,
    StreamChunk,
    error_from_exception,
    error_from_status,
    iter_sse,
)


class OpenAICompatibleProvider(Provider):
    default_base_url = "https://api.openai.com/v1"

    def _url(self) -> str:
        return (self.config.base_url or self.default_base_url).rstrip("/") + "/chat/completions"

    def _headers(self) -> dict[str, str]:
        key = self.config.api_key()
        return {"Authorization": f"Bearer {key}"} if key else {}

    def _payload(self, request: CompletionRequest, stream: bool) -> dict:
        body: dict = {"model": self.config.model, "messages": request.messages, "max_tokens": request.max_tokens}
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if stream:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
        return body

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        assert self.client is not None
        try:
            resp = await self.client.post(
                self._url(), json=self._payload(request, False), headers=self._headers(), timeout=self.config.timeout_s
            )
            if resp.status_code >= 400:
                raise error_from_status(self.name, resp.status_code, resp.text)
            data = resp.json()
            choice = data["choices"][0]
            usage = data.get("usage") or {}
            content = choice["message"].get("content") or ""
            return CompletionResult(
                content=content,
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
                model=data.get("model", self.config.model),
                finish_reason=choice.get("finish_reason") or "stop",
            )
        except ProviderError:
            raise
        except Exception as exc:
            raise error_from_exception(self.name, exc) from exc

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        assert self.client is not None
        try:
            async with self.client.stream(
                "POST",
                self._url(),
                json=self._payload(request, True),
                headers=self._headers(),
                timeout=self.config.timeout_s,
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode(errors="replace")
                    raise error_from_status(self.name, resp.status_code, body)
                async for _event, data in iter_sse(resp):
                    if data.strip() == "[DONE]":
                        break
                    obj = json.loads(data)
                    usage = obj.get("usage")
                    chunk = StreamChunk()
                    if obj.get("choices"):
                        choice = obj["choices"][0]
                        chunk.delta = (choice.get("delta") or {}).get("content") or ""
                        chunk.finish_reason = choice.get("finish_reason")
                    if usage:
                        chunk.prompt_tokens = usage.get("prompt_tokens")
                        chunk.completion_tokens = usage.get("completion_tokens")
                    yield chunk
        except ProviderError:
            raise
        except httpx.HTTPError as exc:
            raise error_from_exception(self.name, exc) from exc
        except (ValueError, KeyError) as exc:
            raise error_from_exception(self.name, exc) from exc


class OllamaProvider(OpenAICompatibleProvider):
    default_base_url = "http://localhost:11434/v1"
