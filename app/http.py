"""Outbound HTTP client used for provider calls.

httpx's connection pool degrades sharply under high concurrency: in our benchmark against the
mock provider it fell from ~200 req/s at 10 concurrent requests to ~90 req/s at 200, while
aiohttp scaled to >3,000 req/s. The gateway therefore uses aiohttp in production behind the small
interface the provider adapters need (``post``, ``stream``, ``aclose``), which httpx.AsyncClient
also satisfies - tests keep using httpx with MockTransport.

aiohttp errors are re-raised as the equivalent httpx exceptions so provider error classification
(retryable timeout / connection error) stays in one place.

Redirects are never followed (as with httpx's default): aiohttp would forward custom credential
headers such as Anthropic's ``x-api-key`` to the redirect target, including other origins. A 3xx
from a provider surfaces as a non-retryable error instead.
"""

from __future__ import annotations

import asyncio
import json as jsonlib
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Protocol
from urllib.parse import urlsplit
from urllib.request import getproxies, proxy_bypass

import httpx


class HttpClient(Protocol):
    """What provider adapters need from an HTTP client. Satisfied by AiohttpClient and httpx.AsyncClient."""

    async def post(self, url: str, *, json: Any, headers: dict[str, str], timeout: float) -> Any: ...

    def stream(
        self, method: str, url: str, *, json: Any, headers: dict[str, str], timeout: float
    ) -> AbstractAsyncContextManager[Any]: ...

    async def aclose(self) -> None: ...


class HttpResponse:
    def __init__(self, status_code: int, body: bytes) -> None:
        self.status_code = status_code
        self.content = body

    @property
    def text(self) -> str:
        return self.content.decode(errors="replace")

    def json(self) -> Any:
        return jsonlib.loads(self.content)


class StreamResponse:
    def __init__(self, resp) -> None:
        self._resp = resp
        self.status_code = resp.status

    async def aread(self) -> bytes:
        return await self._resp.read()

    async def aiter_lines(self) -> AsyncIterator[str]:
        async for raw in self._resp.content:
            yield raw.decode(errors="replace").rstrip("\r\n")


def _translate(exc: BaseException) -> Exception:
    import aiohttp

    if isinstance(exc, (asyncio.TimeoutError, aiohttp.ServerTimeoutError)):
        return httpx.ReadTimeout(str(exc) or "timeout")
    if isinstance(exc, aiohttp.ClientError):
        return httpx.ConnectError(str(exc) or type(exc).__name__)
    return exc  # type: ignore[return-value]


def _reject_https_scheme_proxies() -> None:
    """aiohttp ignores https:// proxy URLs from the environment (logging a warning) and would then
    connect directly, bypassing a proxy the deployment requires. Fail at startup instead."""
    bad = {k: v for k, v in getproxies().items() if k in ("http", "https") and v.lower().startswith("https://")}
    if bad:
        raise ValueError(
            f"Unsupported proxy configuration {sorted(bad)}: https:// proxy URLs are not supported by the "
            "provider HTTP client. Use an http:// proxy URL (traffic to providers is still TLS end to end "
            "through the CONNECT tunnel), or unset the variable."
        )


class AiohttpClient:
    """Lazily creates one pooled aiohttp session inside the running event loop."""

    def __init__(self, max_connections: int = 1000, max_per_host: int = 0) -> None:
        self.max_connections, self.max_per_host = max_connections, max_per_host
        self._session = None
        _reject_https_scheme_proxies()
        # Resolve HTTP(S)_PROXY / NO_PROXY ourselves instead of aiohttp's trust_env, which also applies
        # .netrc credentials (and in some versions lets them replace an explicit Authorization header).
        self._proxies = {k: v for k, v in getproxies().items() if k in ("http", "https")}
        self._proxy_cache: dict[tuple[str, str, int | None], str | None] = {}

    def proxy_for(self, url: str) -> str | None:
        """Proxy URL for a request, honouring NO_PROXY (same semantics as httpx/urllib)."""
        parts = urlsplit(url)
        host = parts.hostname or ""
        key = (parts.scheme, host, parts.port)
        if key not in self._proxy_cache:
            proxy = self._proxies.get(parts.scheme)
            # Pass host:port so port-qualified NO_PROXY entries (internal.example:8443) match.
            target = f"{host}:{parts.port}" if parts.port else host
            self._proxy_cache[key] = None if not proxy or proxy_bypass(target) else proxy
        return self._proxy_cache[key]

    def _get_session(self):
        if self._session is None or self._session.closed:
            import aiohttp

            connector = aiohttp.TCPConnector(
                limit=self.max_connections, limit_per_host=self.max_per_host, ttl_dns_cache=300, keepalive_timeout=30
            )
            self._session = aiohttp.ClientSession(connector=connector, raise_for_status=False, trust_env=False)
        return self._session

    async def post(self, url: str, *, json: Any, headers: dict[str, str], timeout: float) -> HttpResponse:
        import aiohttp

        try:
            async with self._get_session().post(
                url,
                json=json,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=False,
                proxy=self.proxy_for(url),
            ) as resp:
                if 300 <= resp.status < 400:
                    # Redirects are rejected by status alone; never wait on a (possibly stalled) body.
                    return HttpResponse(resp.status, b"")
                return HttpResponse(resp.status, await resp.read())
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise _translate(exc) from exc

    @asynccontextmanager
    async def stream(
        self, method: str, url: str, *, json: Any, headers: dict[str, str], timeout: float
    ) -> AsyncIterator[StreamResponse]:
        import aiohttp

        # No total timeout for streams: bound connect and each read instead, so long generations are allowed.
        client_timeout = aiohttp.ClientTimeout(total=None, connect=timeout, sock_read=timeout)
        try:
            async with self._get_session().request(
                method,
                url,
                json=json,
                headers=headers,
                timeout=client_timeout,
                allow_redirects=False,
                proxy=self.proxy_for(url),
            ) as resp:
                yield StreamResponse(resp)
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise _translate(exc) from exc

    async def aclose(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
