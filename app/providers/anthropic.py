"""Anthropic Messages API adapter."""

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

ANTHROPIC_VERSION = "2023-06-01"


class AnthropicProvider(Provider):
    default_base_url = "https://api.anthropic.com"

    def _url(self) -> str:
        return (self.config.base_url or self.default_base_url).rstrip("/") + "/v1/messages"

    def _headers(self) -> dict[str, str]:
        headers = {"anthropic-version": ANTHROPIC_VERSION}
        key = self.config.api_key()
        if key:
            headers["x-api-key"] = key
        return headers

    def _payload(self, request: CompletionRequest, stream: bool) -> dict:
        system = "\n\n".join(m["content"] for m in request.messages if m["role"] == "system")
        messages = [
            {"role": m["role"] if m["role"] in ("user", "assistant") else "user", "content": m["content"]}
            for m in request.messages
            if m["role"] != "system"
        ]
        body: dict = {"model": self.config.model, "max_tokens": request.max_tokens, "messages": messages}
        if system:
            body["system"] = system
        if request.temperature is not None:
            body["temperature"] = min(request.temperature, 1.0)
        if stream:
            body["stream"] = True
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
            text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
            usage = data.get("usage") or {}
            return CompletionResult(
                content=text,
                prompt_tokens=int(usage.get("input_tokens", 0)),
                completion_tokens=int(usage.get("output_tokens", 0)),
                model=data.get("model", self.config.model),
                finish_reason=_finish(data.get("stop_reason")),
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
                async for event, data in iter_sse(resp):
                    obj = json.loads(data)
                    etype = obj.get("type", event)
                    if etype == "message_start":
                        usage = obj.get("message", {}).get("usage", {})
                        yield StreamChunk(prompt_tokens=usage.get("input_tokens"))
                    elif etype == "content_block_delta":
                        yield StreamChunk(delta=obj.get("delta", {}).get("text", ""))
                    elif etype == "message_delta":
                        yield StreamChunk(
                            completion_tokens=obj.get("usage", {}).get("output_tokens"),
                            finish_reason=_finish(obj.get("delta", {}).get("stop_reason")),
                        )
                    elif etype == "error":
                        err = obj.get("error", {})
                        retryable = err.get("type") in ("overloaded_error", "api_error", "rate_limit_error")
                        raise ProviderError(
                            self.name, err.get("type", "error"), err.get("message", ""), retryable=retryable
                        )
        except ProviderError:
            raise
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise error_from_exception(self.name, exc) from exc


def _finish(reason: str | None) -> str:
    return {"end_turn": "stop", "max_tokens": "length", "stop_sequence": "stop"}.get(reason or "", reason or "stop")
