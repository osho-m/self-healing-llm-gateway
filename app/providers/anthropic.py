"""Anthropic Messages API adapter, built on the official ``anthropic`` SDK.

The gateway owns retries, backoff and circuit breaking, so the SDK client runs with
``max_retries=0``: SDK retries stacked inside the gateway's would multiply attempts and skew the
breaker. The SDK's aiohttp backend is used for the same reason the rest of the gateway uses
aiohttp: httpx-based clients degrade under high concurrency.

Sampling parameters (``temperature``) are not forwarded: current Claude models reject them.
Refusals (``stop_reason == "refusal"``) surface as non-retryable content-policy errors rather than
empty successes. Server-side model fallbacks are not enabled; the gateway's tier chain decides
where a request goes next, and keeps cost accounting per provider.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import aiohttp
import anthropic
import httpx2

from app.providers.base import (
    CompletionRequest,
    CompletionResult,
    Provider,
    ProviderError,
    StreamChunk,
    error_from_status,
)

# Error types the API reports inside an HTTP-200 event stream that are worth retrying elsewhere.
_RETRYABLE_STREAM_ERRORS = {
    "overloaded_error": "server_error",
    "api_error": "server_error",
    "rate_limit_error": "rate_limited",
    "timeout_error": "timeout",
}


class _RedirectRejected(Exception):
    """Raised from a response hook as soon as a 3xx arrives, before the SDK reads its body."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status} redirect not followed")
        self.status = status


async def _reject_redirects(response: httpx2.Response) -> None:
    # Runs on headers. Reading a redirect's body first would let a stalled body turn a
    # non-retryable misconfiguration into a retryable timeout.
    if 300 <= response.status_code < 400:
        raise _RedirectRejected(response.status_code)


def _redirect_cause(exc: BaseException) -> _RedirectRejected | None:
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, _RedirectRejected):
            return seen
        seen = seen.__cause__ or seen.__context__
    return None


_TRANSPORT_ERRORS = (
    anthropic.AnthropicError,
    httpx2.TransportError,
    aiohttp.ClientError,
    TimeoutError,
    json.JSONDecodeError,
    _RedirectRejected,
)

_FINISH = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length", "tool_use": "tool_calls"}


class AnthropicProvider(Provider):
    _sdk: anthropic.AsyncAnthropic | None = None

    def _client(self) -> anthropic.AsyncAnthropic:
        # Created lazily inside the running event loop (the aiohttp session needs one).
        if self._sdk is None:
            key = self.config.api_key()
            if not key:
                raise ProviderError(
                    self.name, "auth", f"{self.config.api_key_env or 'API key'} is not set", retryable=False
                )
            self._sdk = anthropic.AsyncAnthropic(
                api_key=key,
                base_url=self.config.base_url or None,
                max_retries=0,
                timeout=self.config.timeout_s,
                # Never follow redirects: a 307/308 would resend the full prompt to the target.
                # The hook rejects a 3xx on its headers, before any body is read.
                http_client=anthropic.DefaultAioHttpClient(
                    follow_redirects=False, event_hooks={"response": [_reject_redirects]}
                ),
            )
        return self._sdk

    def _params(self, request: CompletionRequest) -> dict[str, Any]:
        system = "\n\n".join(m["content"] for m in request.messages if m["role"] == "system")
        messages = [
            {"role": m["role"] if m["role"] in ("user", "assistant") else "user", "content": m["content"]}
            for m in request.messages
            if m["role"] != "system"
        ]
        params: dict[str, Any] = {
            "model": self.config.model,
            # The gateway already applied min_max_tokens, capped at what admission reserved.
            "max_tokens": request.max_tokens,
            "messages": messages,
        }
        if system:
            params["system"] = system
        if self.config.effort:
            params["output_config"] = {"effort": self.config.effort}
        return params

    def _error(self, exc: Exception) -> ProviderError:
        redirect = _redirect_cause(exc)
        if redirect is not None:
            return ProviderError(
                self.name, "unexpected_redirect", str(redirect), retryable=False, status=redirect.status
            )
        if isinstance(exc, json.JSONDecodeError):
            return ProviderError(self.name, "bad_response", f"malformed JSON from provider: {exc}", retryable=True)
        # Most specific first. APITimeoutError subclasses APIConnectionError.
        if isinstance(exc, anthropic.APITimeoutError):
            return ProviderError(self.name, "timeout", str(exc) or "timeout", retryable=True)
        if isinstance(exc, anthropic.APIConnectionError):
            return ProviderError(self.name, "connection", str(exc) or "connection error", retryable=True)
        if isinstance(exc, anthropic.APIStatusError):
            if exc.status_code < 300:
                # An error event inside a successful (200) stream: classify by its structured type.
                error_type = getattr(exc, "type", None) or ""
                kind = _RETRYABLE_STREAM_ERRORS.get(error_type)
                if kind:
                    return ProviderError(self.name, kind, error_type, retryable=True)
                return ProviderError(self.name, error_type or "stream_error", str(exc.message), retryable=False)
            return error_from_status(self.name, exc.status_code, str(exc.message))
        # Transport failures the SDK passes through unchanged (mostly during stream iteration).
        if isinstance(exc, (httpx2.TimeoutException, TimeoutError)):
            return ProviderError(self.name, "timeout", str(exc) or "timeout", retryable=True)
        if isinstance(exc, (httpx2.TransportError, aiohttp.ClientError)):
            return ProviderError(self.name, "connection", str(exc) or type(exc).__name__, retryable=True)
        return ProviderError(self.name, "unknown", repr(exc), retryable=False)

    def _refusal(self, details: Any) -> ProviderError:
        category = getattr(details, "category", None) or "unspecified"
        return ProviderError(self.name, "content_policy", f"model refused (category: {category})", retryable=False)

    def _tokens(self, value: Any) -> int:
        # The SDK does not validate response models strictly; a bad count must not break settlement.
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        raise ProviderError(self.name, "bad_response", f"invalid usage count {value!r}", retryable=True)

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        try:
            message = await self._client().messages.create(**self._params(request))
        except _TRANSPORT_ERRORS as exc:
            raise self._error(exc) from exc
        if message.stop_reason == "refusal":
            raise self._refusal(message.stop_details)
        text = "".join(block.text for block in message.content if block.type == "text")
        return CompletionResult(
            content=text,
            prompt_tokens=self._tokens(message.usage.input_tokens),
            completion_tokens=self._tokens(message.usage.output_tokens),
            model=message.model,
            finish_reason=_FINISH.get(message.stop_reason or "", message.stop_reason or "stop"),
        )

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        try:
            async with self._client().messages.stream(**self._params(request)) as stream:
                async for event in stream:
                    if event.type == "message_start":
                        yield StreamChunk(prompt_tokens=self._tokens(event.message.usage.input_tokens))
                    elif event.type == "content_block_delta" and event.delta.type == "text_delta":
                        yield StreamChunk(delta=event.delta.text)
                    elif event.type == "message_delta":
                        if event.delta.stop_reason == "refusal":
                            raise self._refusal(getattr(event.delta, "stop_details", None))
                        reason = event.delta.stop_reason or ""
                        yield StreamChunk(
                            completion_tokens=self._tokens(event.usage.output_tokens),
                            finish_reason=_FINISH.get(reason, reason or "stop"),
                        )
        except ProviderError:
            raise
        except _TRANSPORT_ERRORS as exc:
            raise self._error(exc) from exc

    async def aclose(self) -> None:
        if self._sdk is not None:
            await self._sdk.close()
            self._sdk = None
