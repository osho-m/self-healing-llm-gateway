import json
import re
import time

import httpx
import pytest

from tests.conftest import BASE_POLICY, admin, chat, policy_dict


def sse_events(text: str) -> list:
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ") and line != "data: [DONE]"]


# ---------------------------------------------------------------- auth & routing


def test_requires_valid_key(client):
    assert (
        client.post(
            "/v1/chat/completions", json={"model": "balanced", "messages": [{"role": "user", "content": "x"}]}
        ).status_code
        == 401
    )
    r = chat(client, key="nope")
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_api_key"


def test_bearer_auth_and_basic_completion(client):
    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer alpha-key"},
        json={"model": "balanced", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["x_gateway"]["provider"] == "primary" and body["x_gateway"]["fallback_used"] is False
    assert body["usage"]["total_tokens"] == body["usage"]["prompt_tokens"] + body["usage"]["completion_tokens"]
    assert r.headers["X-Request-ID"] and r.headers["X-Gateway-Provider"] == "primary"


def test_tier_restrictions(client):
    assert chat(client, tier="premium").status_code == 403
    assert chat(client, tier="ultra").status_code == 404
    models = client.get("/v1/models", headers={"X-API-Key": "alpha-key"}).json()["data"]
    assert [m["id"] for m in models] == ["balanced", "solo"]


def test_validation_errors_are_openai_shaped(client):
    r = client.post(
        "/v1/chat/completions", headers={"X-API-Key": "alpha-key"}, json={"model": "balanced", "messages": []}
    )
    assert r.status_code == 422 and r.json()["error"]["type"] == "invalid_request"


# ---------------------------------------------------------------- rate limits & budgets


def test_rate_limit_returns_429_with_retry_after(make_client):
    data = policy_dict()
    data["teams"][0]["requests_per_minute"] = 2
    client = make_client(data)
    assert chat(client).status_code == 200
    assert chat(client).status_code == 200
    r = chat(client)
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limit_exceeded"
    assert int(r.headers["Retry-After"]) >= 1


def test_budget_warning_then_block(make_client):
    data = policy_dict()
    # Each request reserves at most (prompt + 512 max_tokens) * $2/Mtok ~= $0.001.
    data["teams"][0]["daily_budget_usd"] = 0.0015
    data["teams"][0]["budget_warning_ratio"] = 0.01
    client = make_client(data)
    first = chat(client)
    assert first.status_code == 200 and "X-Budget-Warning" in first.headers
    blocked = None
    for _ in range(300):
        r = chat(client)
        if r.status_code != 200:
            blocked = r
            break
    assert blocked is not None and blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "budget_exceeded" and "Retry-After" in blocked.headers
    status = admin(client, "GET", "/v1/admin/teams/alpha/status").json()
    assert status["budget"]["spent_today_usd"] <= 0.0015
    assert status["budget"]["alert"] in ("warning", "blocked")


def test_admin_can_raise_budget_without_restart(make_client):
    data = policy_dict()
    data["teams"][0]["daily_budget_usd"] = 0
    client = make_client(data)
    assert chat(client).json()["error"]["code"] == "budget_exceeded"
    r = admin(client, "PATCH", "/v1/admin/teams/alpha", json={"daily_budget_usd": 10})
    assert r.status_code == 200 and r.json()["runtime_overrides"] == {"daily_budget_usd": 10}
    assert chat(client).status_code == 200
    audit = admin(client, "GET", "/v1/admin/audit").json()
    assert audit[0]["action"] == "update_team" and audit[0]["actor"] == "admin-key"
    admin(client, "DELETE", "/v1/admin/teams/alpha/overrides")
    assert chat(client).status_code == 429


# ---------------------------------------------------------------- resilience


def test_outage_triggers_retry_then_fallback_and_circuit(client):
    admin(client, "POST", "/v1/admin/providers/primary/simulate-outage", params={"enabled": "true"})
    r = chat(client)
    meta = r.json()["x_gateway"]
    assert r.status_code == 200 and meta["provider"] == "secondary" and meta["fallback_used"]
    assert meta["attempts"] == ["primary: server_error"] * 3
    providers = admin(client, "GET", "/v1/admin/providers").json()
    assert providers["primary"]["circuit"] == "open" and providers["primary"]["status"] == "down"
    # Circuit open: next request skips primary entirely.
    meta2 = chat(client).json()["x_gateway"]
    assert meta2["attempts"] == ["primary: circuit open"] and meta2["provider"] == "secondary"
    metrics = client.get("/metrics").text
    assert 'gateway_fallbacks_total{from_provider="primary",tier="balanced",to_provider="secondary"} 2.0' in metrics
    assert 'gateway_circuit_state{provider="primary"} 2.0' in metrics


def test_circuit_half_open_recovers(make_client):
    data = policy_dict()
    data["resilience"]["circuit_cooldown_s"] = 0.05
    client = make_client(data)
    admin(client, "POST", "/v1/admin/providers/primary/simulate-outage", params={"enabled": "true"})
    chat(client)
    admin(client, "POST", "/v1/admin/providers/primary/simulate-outage", params={"enabled": "false"})
    time.sleep(0.08)
    r = chat(client)
    assert r.json()["x_gateway"]["provider"] == "primary"
    assert admin(client, "GET", "/v1/admin/providers").json()["primary"]["circuit"] == "closed"


def test_health_probe_closes_half_open_circuit(make_client):
    data = policy_dict()
    data["resilience"]["circuit_cooldown_s"] = 0.05
    client = make_client(data)
    admin(client, "POST", "/v1/admin/providers/primary/simulate-outage", params={"enabled": "true"})
    chat(client)
    admin(client, "POST", "/v1/admin/providers/primary/simulate-outage", params={"enabled": "false"})
    time.sleep(0.08)
    probe = admin(client, "POST", "/v1/admin/providers/primary/probe").json()
    assert probe["circuit"] == "closed"


def test_all_providers_down_returns_503(client):
    admin(client, "POST", "/v1/admin/providers/primary/simulate-outage", params={"enabled": "true"})
    r = chat(client, tier="solo")
    assert r.status_code == 503 and r.json()["error"]["code"] == "all_providers_unavailable"
    assert "Retry-After" in r.headers
    assert client.get("/readyz").status_code == 503


def test_non_retryable_error_does_not_fall_back(make_client):
    data = policy_dict()
    data["providers"][0] = {"name": "primary", "kind": "openai", "base_url": "http://primary.test/v1", "model": "x"}
    calls = []

    def handler(request: httpx.Request):
        calls.append(request.url.host)
        return httpx.Response(401, json={"error": {"message": "invalid key"}})

    client = make_client(data, transport=httpx.MockTransport(handler))
    r = chat(client)
    assert r.status_code == 502 and r.json()["error"]["code"] == "upstream_error"
    assert calls == ["primary.test"]  # no retries, no fallback
    assert admin(client, "GET", "/v1/admin/providers").json()["primary"]["circuit"] == "closed"


def test_retryable_http_errors_retry_with_backoff_then_succeed(make_client):
    data = policy_dict()
    data["providers"][0] = {"name": "primary", "kind": "openai", "base_url": "http://primary.test/v1", "model": "x"}
    data["resilience"]["circuit_failure_threshold"] = 10
    responses = iter(
        [
            httpx.Response(429, text="slow"),
            httpx.Response(503, text="busy"),
            httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            ),
        ]
    )
    client = make_client(data, transport=httpx.MockTransport(lambda r: next(responses)))
    meta = chat(client).json()["x_gateway"]
    assert meta["provider"] == "primary" and meta["attempts"] == ["primary: rate_limited", "primary: server_error"]


# ---------------------------------------------------------------- streaming


def test_streaming_passthrough_matches_non_streaming(client):
    expected = chat(client, content="stream me please").json()["choices"][0]["message"]["content"]
    r = chat(client, content="stream me please", stream=True)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = sse_events(r.text)
    text = "".join(e["choices"][0]["delta"].get("content", "") for e in events if "choices" in e)
    assert text == expected
    assert r.text.rstrip().endswith("data: [DONE]")
    meta = next(e["x_gateway"] for e in events if "x_gateway" in e)
    assert meta["usage"]["total_tokens"] > 0 and meta["cost_usd"] > 0


def test_streaming_fails_over_before_first_byte(client):
    admin(client, "POST", "/v1/admin/providers/primary/simulate-outage", params={"enabled": "true"})
    r = chat(client, stream=True)
    assert r.status_code == 200 and r.headers["X-Gateway-Provider"] == "secondary"
    meta = next(e["x_gateway"] for e in sse_events(r.text) if "x_gateway" in e)
    assert meta["fallback_used"] is True


def test_streaming_all_down_returns_503_not_stream(client):
    admin(client, "POST", "/v1/admin/providers/primary/simulate-outage", params={"enabled": "true"})
    assert chat(client, tier="solo", stream=True).status_code == 503


# ---------------------------------------------------------------- enrichment


def test_enrichment_system_prompt_blocklist_and_disclaimer(make_client):
    data = policy_dict()
    data["providers"][0] = {"name": "primary", "kind": "openai", "base_url": "http://primary.test/v1", "model": "x"}
    data["teams"][0]["enrichment"] = {
        "system_prompt": "SYS",
        "disclaimer": "DISCLAIMER",
        "blocked_terms": ["Secret-Project"],
    }
    seen = {}

    def handler(request):
        seen["messages"] = json.loads(request.content)["messages"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "answer"}}], "usage": {}})

    client = make_client(data, transport=httpx.MockTransport(handler))
    r = chat(client)
    assert r.json()["choices"][0]["message"]["content"] == "answer\n\nDISCLAIMER"
    assert seen["messages"][0] == {"role": "system", "content": "SYS"}
    blocked = chat(client, content="tell me about secret-project")
    assert blocked.status_code == 400 and blocked.json()["error"]["code"] == "content_policy_violation"


# ---------------------------------------------------------------- admin


def test_admin_rbac(client):
    assert client.get("/v1/admin/providers").status_code == 401
    assert client.get("/v1/admin/providers", headers={"X-API-Key": "alpha-key"}).status_code == 403
    assert client.get("/v1/admin/providers", headers={"X-API-Key": "ops-key"}).status_code == 200
    assert client.get("/v1/admin/providers", headers={"X-Admin-Key": "wrong"}).status_code == 401
    r = client.post("/v1/admin/providers/primary/simulate-outage?enabled=true", headers={"X-API-Key": "ops-key"})
    assert r.status_code == 200
    assert admin(client, "GET", "/v1/admin/audit").json()[0]["actor"] == "team:ops"


def test_admin_rejects_invalid_override(client):
    assert admin(client, "PATCH", "/v1/admin/teams/alpha", json={"allowed_tiers": ["ghost"]}).status_code == 422
    assert admin(client, "PATCH", "/v1/admin/teams/alpha", json={}).status_code == 400
    assert admin(client, "PATCH", "/v1/admin/teams/nobody", json={"requests_per_minute": 1}).status_code == 404


def test_metrics_and_health_endpoints(client):
    chat(client)
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json()["ready"] is True
    text = client.get("/metrics").text
    for name in (
        "gateway_requests_total",
        "gateway_tokens_total",
        "gateway_cost_usd_total",
        "gateway_overhead_seconds_bucket",
        "gateway_budget_utilization_ratio",
        "gateway_provider_health",
    ):
        assert name in text


def test_base_policy_unchanged_by_tests():
    assert BASE_POLICY["teams"][0]["requests_per_minute"] == 1000


def test_stream_overhead_excludes_provider_time(make_client):
    data = policy_dict()
    data["providers"][0]["mock_latency_ms"] = 200
    client = make_client(data)
    assert chat(client, stream=True).status_code == 200
    metrics = client.get("/metrics").text
    count = float(re.search(r"^gateway_overhead_seconds_count ([0-9.e+]+)$", metrics, re.M).group(1))
    total = float(re.search(r"^gateway_overhead_seconds_sum ([0-9.e+]+)$", metrics, re.M).group(1))
    assert count == 1 and total < 0.1


def test_request_id_echo_and_internal_error_shape(client):
    r = client.get("/healthz", headers={"X-Request-ID": "trace-me-123"})
    assert r.headers["X-Request-ID"] == "trace-me-123"

    async def boom(*_a, **_k):
        raise RuntimeError("unexpected")

    client.app.state.gateway.admit = boom
    r = chat(client)
    assert r.status_code == 500 and r.json()["error"]["code"] == "internal_error"
    assert r.headers["X-Request-ID"]


def test_unconfigured_provider_is_skipped_visibly(make_client, monkeypatch):
    monkeypatch.delenv("UNSET_CLAUDE_KEY", raising=False)
    data = policy_dict()
    data["providers"].insert(
        0, {"name": "claude", "kind": "anthropic", "model": "claude-opus-5-5", "api_key_env": "UNSET_CLAUDE_KEY"}
    )
    data["tiers"]["balanced"] = ["claude", "primary", "secondary"]
    data["tiers"]["solo"] = ["claude"]
    client = make_client(data)
    meta = chat(client).json()["x_gateway"]
    assert meta["provider"] == "primary" and meta["attempts"] == ["claude: not configured"]
    report = admin(client, "GET", "/v1/admin/providers").json()["claude"]
    assert report["configured"] is False and report["status"] == "not_configured"
    r = chat(client, tier="solo")
    assert r.status_code == 503  # a tier with only unconfigured providers cannot serve
    assert client.get("/readyz").json()["tiers"]["solo"] is False


def test_production_refuses_tier_without_configured_provider(policy_file, monkeypatch):
    from app.main import create_app
    from app.policy import PolicyManager, hash_key
    from app.settings import Settings

    monkeypatch.delenv("UNSET_CLAUDE_KEY", raising=False)
    data = policy_dict()
    for team in data["teams"]:
        team["api_key_sha256"] = [hash_key(k) for k in team.pop("api_keys")]
    data["providers"].append({"name": "claude", "kind": "anthropic", "model": "m", "api_key_env": "UNSET_CLAUDE_KEY"})
    data["tiers"]["solo"] = ["claude"]
    settings = Settings(
        environment="production",
        admin_api_key="k" * 32,
        redis_url="redis://x",
        audit_log_path=None,
        health_checks_enabled=False,
    )
    with pytest.raises(ValueError, match="no configured provider"):
        create_app(settings, policy=PolicyManager(policy_file(data), forbid_plaintext_keys=True))


def test_admission_reserves_provider_max_tokens_floor(make_client, monkeypatch):
    monkeypatch.setenv("SET_CLAUDE_KEY", "sk-test")
    data = policy_dict()
    data["providers"].insert(
        0,
        {"name": "claude", "kind": "anthropic", "model": "m", "api_key_env": "SET_CLAUDE_KEY", "min_max_tokens": 1024},
    )
    data["tiers"]["balanced"] = ["claude", "primary"]
    data["teams"][0]["tokens_per_minute"] = 1000
    client = make_client(data)
    # The client asks for 1 token, but the provider would raise it to 1024: admission must count 1024.
    r = chat(client, max_tokens=1)
    assert r.status_code == 429 and "1000/min" in r.json()["error"]["message"]


async def test_retired_adapter_closes_only_after_in_flight_calls_finish():
    from app.policy import ProviderConfig
    from app.providers.mock import MockProvider

    closed = []

    class Tracked(MockProvider):
        async def aclose(self):
            closed.append(True)

    p = Tracked(ProviderConfig(name="m", kind="mock"))
    p.acquire()
    await p.retire()
    assert closed == []  # still in use
    await p.release()
    assert closed == [True]
