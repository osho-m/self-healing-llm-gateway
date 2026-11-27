"""Prometheus metrics, OpenTelemetry tracing, and structured JSON logging."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

from opentelemetry import trace
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)
OVERHEAD_BUCKETS = (0.0005, 0.001, 0.0025, 0.005, 0.0075, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0)
HEALTH_STATUSES = ("healthy", "degraded", "down")


class Metrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        r = self.registry
        self.requests = Counter(
            "gateway_requests_total",
            "Requests by outcome",
            ["team", "tier", "provider", "status", "priority"],
            registry=r,
        )
        self.errors = Counter(
            "gateway_errors_total", "Errors by type", ["team", "tier", "provider", "error_type"], registry=r
        )
        self.latency = Histogram(
            "gateway_request_duration_seconds",
            "End-to-end latency",
            ["tier", "provider"],
            buckets=LATENCY_BUCKETS,
            registry=r,
        )
        self.provider_latency = Histogram(
            "gateway_provider_duration_seconds",
            "Provider call latency",
            ["provider"],
            buckets=LATENCY_BUCKETS,
            registry=r,
        )
        self.overhead = Histogram(
            "gateway_overhead_seconds", "Gateway time excluding provider calls", buckets=OVERHEAD_BUCKETS, registry=r
        )
        self.tokens = Counter("gateway_tokens_total", "Tokens processed", ["team", "provider", "direction"], registry=r)
        self.cost = Counter("gateway_cost_usd_total", "Spend in USD", ["team", "provider"], registry=r)
        self.fallbacks = Counter(
            "gateway_fallbacks_total", "Fallback activations", ["tier", "from_provider", "to_provider"], registry=r
        )
        self.retries = Counter("gateway_retries_total", "Provider retries", ["provider", "error_type"], registry=r)
        self.circuit_state = Gauge("gateway_circuit_state", "0=closed 1=half_open 2=open", ["provider"], registry=r)
        self.circuit_changes = Counter(
            "gateway_circuit_state_changes_total", "Circuit transitions", ["provider", "to_state"], registry=r
        )
        self.provider_health = Gauge(
            "gateway_provider_health", "1 for the provider's current status", ["provider", "status"], registry=r
        )
        self.rate_limited = Counter(
            "gateway_rate_limited_total", "Requests rejected by limits", ["team", "limit", "priority"], registry=r
        )
        self.budget_utilization = Gauge("gateway_budget_utilization_ratio", "Daily spend / cap", ["team"], registry=r)
        self.budget_spend = Gauge("gateway_budget_spend_usd", "Daily spend so far", ["team"], registry=r)
        self.budget_warnings = Counter(
            "gateway_budget_warnings_total", "Requests served above the warning ratio", ["team"], registry=r
        )
        self.inflight = Gauge("gateway_inflight_requests", "In-flight requests", registry=r)


tracer = trace.get_tracer("llm-gateway")


def setup_tracing(service_name: str, otlp_endpoint: str | None) -> None:
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider

    if isinstance(trace.get_tracer_provider(), TracerProvider):
        return
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint.rstrip("/") + "/v1/traces"))
        )
    trace.set_tracer_provider(provider)


_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        span = trace.get_current_span().get_span_context()
        if span.is_valid:
            payload["trace_id"] = format(span.trace_id, "032x")
        payload.update({k: v for k, v in record.__dict__.items() if k not in _RESERVED})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(level: str, as_json: bool) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter() if as_json else logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
