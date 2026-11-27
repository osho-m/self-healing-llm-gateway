"""Generate the three provisioned Grafana dashboards (Operations, Business, Performance).

python scripts/gen_dashboards.py   # writes deploy/grafana/dashboards/*.json
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "deploy" / "grafana" / "dashboards"
DS = {"type": "prometheus", "uid": "prometheus"}


def panel(pid, title, exprs, x, y, w=12, h=8, kind="timeseries", unit="short", legend=None, extra=None):
    targets = [
        {"refId": chr(65 + i), "expr": e, "legendFormat": (legend[i] if legend else "__auto"), "datasource": DS}
        for i, e in enumerate(exprs)
    ]
    p = {
        "id": pid,
        "type": kind,
        "title": title,
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": targets,
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "options": {},
    }
    if extra:
        p.update(extra)
    return p


def dashboard(uid, title, panels):
    return {
        "uid": uid,
        "title": title,
        "tags": ["llm-gateway"],
        "timezone": "browser",
        "schemaVersion": 39,
        "refresh": "5s",
        "time": {"from": "now-30m", "to": "now"},
        "panels": panels,
    }


STATE_MAP = {
    "mappings": [
        {
            "type": "value",
            "options": {
                "0": {"text": "CLOSED", "color": "green"},
                "1": {"text": "HALF-OPEN", "color": "yellow"},
                "2": {"text": "OPEN", "color": "red"},
            },
        }
    ]
}

operations = dashboard(
    "gw-operations",
    "LLM Gateway / Operations",
    [
        panel(
            1,
            "Circuit breaker state",
            ["max by (provider) (gateway_circuit_state)"],
            0,
            0,
            w=8,
            kind="stat",
            legend=["{{provider}}"],
            extra={"fieldConfig": {"defaults": STATE_MAP, "overrides": []}},
        ),
        panel(
            2,
            "Provider health (1 = current status)",
            ["gateway_provider_health == 1"],
            8,
            0,
            w=8,
            kind="table",
            legend=["{{provider}} {{status}}"],
        ),
        panel(
            3,
            "Circuit transitions",
            ["sum by (provider, to_state) (increase(gateway_circuit_state_changes_total[5m]))"],
            16,
            0,
            w=8,
            legend=["{{provider}} -> {{to_state}}"],
        ),
        panel(
            4,
            "Requests/s by provider and status",
            ["sum by (provider, status) (rate(gateway_requests_total[1m]))"],
            0,
            8,
            legend=["{{provider}} {{status}}"],
            unit="reqps",
        ),
        panel(
            5,
            "Error rate by provider",
            [
                "sum by (provider) (rate(gateway_errors_total[1m])) / "
                "clamp_min(sum by (provider) (rate(gateway_provider_duration_seconds_count[1m])), 1e-9)"
            ],
            12,
            8,
            legend=["{{provider}}"],
            unit="percentunit",
        ),
        panel(
            6,
            "Errors/s by type",
            ["sum by (provider, error_type) (rate(gateway_errors_total[1m]))"],
            0,
            16,
            legend=["{{provider}} {{error_type}}"],
        ),
        panel(
            7,
            "Fallback events/s",
            ["sum by (tier, from_provider, to_provider) (rate(gateway_fallbacks_total[1m]))"],
            12,
            16,
            legend=["{{tier}}: {{from_provider}} -> {{to_provider}}"],
        ),
        panel(
            8,
            "Retries/s",
            ["sum by (provider, error_type) (rate(gateway_retries_total[1m]))"],
            0,
            24,
            legend=["{{provider}} {{error_type}}"],
        ),
        panel(
            9,
            "Rejected by limits/s",
            ["sum by (team, limit) (rate(gateway_rate_limited_total[1m]))"],
            12,
            24,
            legend=["{{team}} {{limit}}"],
        ),
    ],
)

business = dashboard(
    "gw-business",
    "LLM Gateway / Business",
    [
        panel(
            1,
            "Budget utilization by team",
            ["max by (team) (gateway_budget_utilization_ratio)"],
            0,
            0,
            w=12,
            kind="bargauge",
            legend=["{{team}}"],
            unit="percentunit",
            extra={
                "fieldConfig": {
                    "defaults": {
                        "unit": "percentunit",
                        "min": 0,
                        "max": 1,
                        "thresholds": {
                            "mode": "absolute",
                            "steps": [
                                {"color": "green", "value": None},
                                {"color": "orange", "value": 0.8},
                                {"color": "red", "value": 1},
                            ],
                        },
                    },
                    "overrides": [],
                }
            },
        ),
        panel(
            2,
            "Spend today (USD) by team",
            ["max by (team) (gateway_budget_spend_usd)"],
            12,
            0,
            kind="bargauge",
            legend=["{{team}}"],
            unit="currencyUSD",
        ),
        panel(
            3,
            "Cost rate (USD/hour) by team",
            ["sum by (team) (rate(gateway_cost_usd_total[5m])) * 3600"],
            0,
            8,
            legend=["{{team}}"],
            unit="currencyUSD",
        ),
        panel(
            4,
            "Cost by provider (USD, range)",
            ["sum by (provider) (increase(gateway_cost_usd_total[$__range]))"],
            12,
            8,
            kind="piechart",
            legend=["{{provider}}"],
            unit="currencyUSD",
        ),
        panel(
            5,
            "Requests/min by team",
            ["sum by (team) (rate(gateway_requests_total[1m])) * 60"],
            0,
            16,
            legend=["{{team}}"],
        ),
        panel(
            6,
            "Tokens/min by team",
            ["sum by (team, direction) (rate(gateway_tokens_total[1m])) * 60"],
            12,
            16,
            legend=["{{team}} {{direction}}"],
        ),
        panel(
            7,
            "Budget warnings/min",
            ["sum by (team) (rate(gateway_budget_warnings_total[1m])) * 60"],
            0,
            24,
            legend=["{{team}}"],
        ),
        panel(
            8,
            "Usage by tier and priority",
            ["sum by (tier, priority) (rate(gateway_requests_total[5m])) * 60"],
            12,
            24,
            legend=["{{tier}} {{priority}}"],
        ),
    ],
)

q = "histogram_quantile({q}, sum by (le{by}) (rate({m}_bucket[1m])))"
performance = dashboard(
    "gw-performance",
    "LLM Gateway / Performance",
    [
        panel(
            1,
            "Gateway overhead P50/P95/P99 (excludes provider time)",
            [q.format(q=v, by="", m="gateway_overhead_seconds") for v in (0.5, 0.95, 0.99)],
            0,
            0,
            legend=["p50", "p95", "p99"],
            unit="s",
        ),
        panel(
            2,
            "End-to-end latency P99 by provider",
            [q.format(q=0.99, by=", provider", m="gateway_request_duration_seconds")],
            12,
            0,
            legend=["{{provider}}"],
            unit="s",
        ),
        panel(
            3,
            "Provider latency P50/P95/P99",
            [q.format(q=v, by=", provider", m="gateway_provider_duration_seconds") for v in (0.5, 0.95, 0.99)],
            0,
            8,
            legend=["{{provider}} p50", "{{provider}} p95", "{{provider}} p99"],
            unit="s",
        ),
        panel(
            4,
            "Token throughput (tokens/s)",
            ["sum by (direction) (rate(gateway_tokens_total[1m]))"],
            12,
            8,
            legend=["{{direction}}"],
        ),
        panel(
            5, "Throughput (req/s)", ["sum(rate(gateway_requests_total[1m]))"], 0, 16, legend=["req/s"], unit="reqps"
        ),
        panel(6, "In-flight requests", ["gateway_inflight_requests"], 12, 16, legend=["in-flight"]),
    ],
)

if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, dash in (("operations", operations), ("business", business), ("performance", performance)):
        (OUT / f"{name}.json").write_text(json.dumps(dash, indent=2) + "\n", encoding="utf-8", newline="\n")
        print("wrote", OUT / f"{name}.json")
