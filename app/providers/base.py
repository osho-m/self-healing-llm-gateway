"""Provider abstraction: one normalized contract, many vendor adapters."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx

from app.policy import ProviderConfig

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}


class ProviderError(Exception):
    """Normalized provider failure. ``retryable`` drives retry/fallback decisions."""

    def __init__(self, provider: str, kind: str, message: str, *, retryable: bool, status: int | None = None) -> None:
        super().__init__(f"{provider}: {kind}: {message}")
        self.provider, self.kind, self.retryable, self.status = provider, kind, retryable, status


def error_from_status(provider: str, status: int, body: str) -> ProviderError:
    lowered = body.lower()
    if 300 <= status < 400:
        kind = "unexpected_redirect"  # redirects are not followed, so credentials never leave the origin
    elif status in (401, 403):
        kind = "auth"
    elif "content_policy" in lowered or "content_filter" in lowered or "safety" in lowered:
        kind = "content_policy"
    elif status == 429:
        kind = "rate_limited"
    elif status >= 500:
        kind = "server_error"
    elif status == 408:
        kind = "timeout"
    else:
        kind = "bad_request"
    retryable = status in RETRYABLE_STATUS and kind != "content_policy"
    return ProviderError(provider, kind, body[:300] or f"HTTP {status}", retryable=retryable, status=status)


def error_from_exception(provider: str, exc: Exception) -> ProviderError:
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, httpx.TimeoutException):
        return ProviderError(provider, "timeout", str(exc) or "timeout", retryable=True)
    if isinstance(exc, httpx.TransportError):
        return ProviderError(provider, "connection", str(exc) or type(exc).__name__, retryable=True)
    if isinstance(exc, (ValueError, KeyError, json.JSONDecodeError)):
        return ProviderError(provider, "bad_response", str(exc), retryable=True)
    return ProviderError(provider, "unknown", repr(exc), retryable=False)


@dataclass
class CompletionRequest:
    messages: list[dict[str, str]]
    max_tokens: int
    temperature: float | None = None


@dataclass
class CompletionResult:
    content: str
    prompt_tokens: int
    completion_tokens: int
    model: str
    finish_reason: str = "stop"


@dataclass
class StreamChunk:
    delta: str = ""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str | None = None
    extra: dict = field(default_factory=dict)


class Provider(ABC):
    def __init__(self, config: ProviderConfig, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self.name = config.name
        self.client = client

    @abstractmethod
    async def complete(self, request: CompletionRequest) -> CompletionResult: ...

    @abstractmethod
    def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]: ...

    async def probe(self) -> None:
        """Cheap liveness probe used by the health monitor."""
        await self.complete(CompletionRequest(messages=[{"role": "user", "content": "ping"}], max_tokens=1))


def estimate_tokens(text: str) -> int:
    """Fast, provider-agnostic estimate (~4 chars/token). Used for admission control only."""
    return max(1, (len(text) + 3) // 4)


async def iter_sse(response: httpx.Response) -> AsyncIterator[tuple[str | None, str]]:
    """Yield (event, data) pairs from a server-sent-events response."""
    event: str | None = None
    data: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if data:
                yield event, "\n".join(data)
            event, data = None, []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        yield event, "\n".join(data)
