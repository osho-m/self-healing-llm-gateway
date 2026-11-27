from __future__ import annotations

import copy
import os

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from app.main import create_app
from app.policy import Policy, PolicyManager
from app.settings import Settings
from app.stores import MemoryStore, RedisStore

ADMIN_KEY = "test-admin-key-0123456789abcdef"

BASE_POLICY = {
    "providers": [
        {
            "name": "primary",
            "kind": "mock",
            "model": "m-large",
            "input_price_per_mtok": 1.0,
            "output_price_per_mtok": 2.0,
        },
        {
            "name": "secondary",
            "kind": "mock",
            "model": "m-small",
            "input_price_per_mtok": 0.5,
            "output_price_per_mtok": 1.0,
        },
    ],
    "tiers": {"balanced": ["primary", "secondary"], "premium": ["primary", "secondary"], "solo": ["primary"]},
    "teams": [
        {
            "team_id": "alpha",
            "api_keys": ["alpha-key"],
            "allowed_tiers": ["balanced", "solo"],
            "requests_per_minute": 1000,
            "tokens_per_minute": 1_000_000,
            "daily_budget_usd": 100.0,
        },
        {"team_id": "ops", "api_keys": ["ops-key"], "role": "admin", "allowed_tiers": ["balanced"]},
    ],
    "resilience": {
        "max_retries": 3,
        "backoff_base_s": 0,
        "backoff_max_s": 0,
        "circuit_failure_threshold": 3,
        "circuit_window_s": 30,
        "circuit_cooldown_s": 15,
    },
}


def policy_dict(**changes) -> dict:
    data = copy.deepcopy(BASE_POLICY)
    for key, value in changes.items():
        data[key] = value
    return data


async def _no_sleep(_s: float) -> None:
    return None


@pytest.fixture
def policy_file(tmp_path):
    def write(data: dict | None = None):
        path = tmp_path / "policy.yml"
        path.write_text(yaml.safe_dump(data or BASE_POLICY))
        return path

    return write


@pytest.fixture
def make_client(policy_file, tmp_path):
    clients = []

    def make(data: dict | None = None, *, transport: httpx.MockTransport | None = None, store=None) -> TestClient:
        path = policy_file(data)
        settings = Settings(
            environment="test",
            policy_path=str(path),
            admin_api_key=ADMIN_KEY,
            audit_log_path=str(tmp_path / "audit.log"),
            health_checks_enabled=False,
            policy_reload_interval_s=0,
            log_json=False,
            log_level="WARNING",
        )
        http = httpx.AsyncClient(transport=transport) if transport else None
        app = create_app(
            settings, policy=PolicyManager(path), store=store or MemoryStore(), client=http, sleep=_no_sleep
        )
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def client(make_client):
    return make_client()


def chat(client, key="alpha-key", tier="balanced", content="hello", **extra):
    body = {"model": tier, "messages": [{"role": "user", "content": content}], **extra}
    return client.post("/v1/chat/completions", headers={"X-API-Key": key}, json=body)


def admin(client, method, path, **kw):
    return client.request(method, path, headers={"X-Admin-Key": ADMIN_KEY}, **kw)


def store_params():
    params = [pytest.param("memory", id="memory")]
    if os.environ.get("REDIS_URL") or os.environ.get("REQUIRE_REDIS"):
        params.append(pytest.param("redis", id="redis"))
    return params


@pytest.fixture(params=store_params())
async def store(request):
    if request.param == "memory":
        yield MemoryStore()
        return
    url = os.environ.get("REDIS_URL", "redis://localhost:6379/15")
    s = RedisStore(url, prefix=f"test{os.getpid()}", max_connections=20)  # < test concurrency: must queue, not fail
    if not await s.ping():
        if os.environ.get("REQUIRE_REDIS"):
            pytest.fail(f"REQUIRE_REDIS set but Redis unreachable at {url}")
        pytest.skip("redis unavailable")
    await s.r.flushdb()
    yield s
    await s.r.flushdb()
    await s.close()


def make_policy(**changes) -> Policy:
    return Policy.model_validate(policy_dict(**changes))
