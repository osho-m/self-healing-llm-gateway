"""Application factory and background loops."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from opentelemetry.trace import Status, StatusCode

from app.api import router
from app.audit import AuditLog
from app.errors import GatewayError, gateway_error_handler
from app.gateway import Gateway
from app.observability import Metrics, setup_logging, setup_tracing, tracer
from app.policy import PolicyManager
from app.settings import Settings, get_settings
from app.stores import MemoryStore, RedisStore, Store

log = logging.getLogger(__name__)


_INTERNAL_ERROR = {"error": {"type": "internal_error", "code": "internal_error", "message": "Internal gateway error."}}


class RequestContextMiddleware:
    """Pure ASGI middleware: request id, root span, last-resort 500.

    Avoids Starlette's BaseHTTPMiddleware (``@app.middleware("http")``), which adds a task and
    memory streams to every request and measurably raises per-request CPU.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = next((v for k, v in scope["headers"] if k == b"x-request-id"), b"")
        rid = incoming.decode("latin-1")[:128] or uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = rid
        started = False

        with tracer.start_as_current_span(f"{scope['method']} {scope['path']}") as span:

            async def send_with_id(message) -> None:
                nonlocal started
                if message["type"] == "http.response.start":
                    started = True
                    span.set_attribute("http.status_code", message["status"])
                    headers = list(message.get("headers", []))
                    if not any(k.lower() == b"x-request-id" for k, _ in headers):
                        headers.append((b"x-request-id", rid.encode("latin-1")))
                    message = {**message, "headers": headers}
                await send(message)

            try:
                await self.app(scope, receive, send_with_id)
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR))
                log.exception("unhandled error", extra={"request_id": rid})
                if started:
                    raise
                response = JSONResponse(_INTERNAL_ERROR, status_code=500, headers={"X-Request-ID": rid})
                await response(scope, receive, send)


async def _loop(interval_s: float, fn, name: str) -> None:
    while True:
        await asyncio.sleep(interval_s)
        try:
            await fn()
        except Exception:
            log.exception("background task failed", extra={"task": name})


def create_app(
    settings: Settings | None = None,
    *,
    policy: PolicyManager | None = None,
    store: Store | None = None,
    client: httpx.AsyncClient | None = None,
    sleep=asyncio.sleep,
) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(settings.log_level, settings.log_json)
    setup_tracing(settings.otel_service_name, settings.otel_exporter_otlp_endpoint)

    policy = policy or PolicyManager(settings.policy_path)
    if store is None:
        store = (
            RedisStore(settings.redis_url, max_connections=settings.redis_max_connections)
            if settings.redis_url
            else MemoryStore()
        )
    gateway = Gateway(policy, store, Metrics(), AuditLog(settings.audit_log_path), client=client, sleep=sleep)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = []
        if settings.policy_reload_interval_s > 0:
            tasks.append(
                asyncio.create_task(_loop(settings.policy_reload_interval_s, gateway.refresh_policy, "policy"))
            )
        if settings.health_checks_enabled:
            interval = gateway.policy.resilience.health_check_interval_s
            tasks.append(asyncio.create_task(_loop(interval, gateway.probe_all, "health")))
        await gateway.refresh_policy()
        log.info("gateway started", extra={"environment": settings.environment, "store": type(store).__name__})
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await t
            await gateway.aclose()

    app = FastAPI(
        title="Self-Healing LLM Gateway",
        version="1.0.0",
        description="Per-team auth, rate limits, budgets, retries, circuit breakers, failover and observability.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.gateway = gateway
    app.add_exception_handler(GatewayError, gateway_error_handler)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        return JSONResponse(
            {"error": {"type": "invalid_request", "code": "invalid_request", "message": str(exc.errors()[:5])}},
            status_code=422,
        )

    app.add_middleware(RequestContextMiddleware)
    app.include_router(router)
    return app


def app_factory() -> FastAPI:
    return create_app()


def __getattr__(name: str):  # pragma: no cover - lets `uvicorn app.main:app` work without import-time side effects
    if name == "app":
        globals()["app"] = instance = create_app()
        return instance
    raise AttributeError(name)
