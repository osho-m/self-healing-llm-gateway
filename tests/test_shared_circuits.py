"""Circuit opens shared between gateway replicas through the store (memory and Redis)."""

import asyncio

import pytest

from app.audit import AuditLog
from app.gateway import Gateway
from app.observability import Metrics
from app.policy import PolicyManager
from app.resilience import CLOSED, HALF_OPEN, OPEN, CircuitBreaker
from app.schemas import ChatMessage, ChatRequest
from tests.conftest import policy_dict

REQ = ChatRequest(model="balanced", messages=[ChatMessage(role="user", content="hi")], max_tokens=16)


def replicas(policy_file, store, share: bool, cooldown: float = 30.0) -> tuple[Gateway, Gateway, PolicyManager]:
    data = policy_dict()
    data["resilience"] = {
        "max_retries": 0,
        "backoff_base_s": 0,
        "circuit_failure_threshold": 1,
        "circuit_cooldown_s": cooldown,
        "share_circuit_state": share,
    }
    manager = PolicyManager(policy_file(data))

    async def no_sleep(_s: float) -> None:
        return None

    def make() -> Gateway:
        return Gateway(manager, store, Metrics(), AuditLog(None), sleep=no_sleep)

    return make(), make(), manager


async def settle() -> None:
    for _ in range(5):  # let the background publish task run
        await asyncio.sleep(0)


async def test_an_open_on_one_replica_opens_the_circuit_on_the_other(policy_file, store):
    a, b, manager = replicas(policy_file, store, share=True)
    a.faults.set("primary", outage=True)
    adm = await a.admit(manager.policy.teams[0], REQ)
    await a.route(adm)  # primary fails once (threshold 1) and opens; secondary serves
    assert a.breakers["primary"].current_state() == OPEN
    await settle()

    await b.sync_circuits()
    assert b.breakers["primary"].current_state() == OPEN
    adm = await b.admit(manager.policy.teams[0], REQ)
    provider, _ = await b.route(adm)
    assert provider == "secondary" and "primary: circuit open" in adm.attempts


async def test_without_sharing_each_replica_keeps_its_own_circuit(policy_file, store):
    a, b, manager = replicas(policy_file, store, share=False)
    a.faults.set("primary", outage=True)
    await a.route(await a.admit(manager.policy.teams[0], REQ))
    await settle()
    await b.sync_circuits()
    assert a.breakers["primary"].current_state() == OPEN
    assert b.breakers["primary"].current_state() == CLOSED
    assert await store.open_circuits() == {}  # nothing was published


async def test_republishing_never_extends_the_shared_cooldown(store):
    await store.publish_open("primary", 30)
    first = (await store.open_circuits())["primary"]
    await asyncio.sleep(0.05)
    await store.publish_open("primary", 30)  # e.g. a second replica opening on its own
    assert (await store.open_circuits())["primary"] <= first


def test_adopted_open_keeps_the_original_deadline_and_is_not_republished():
    now = [100.0]
    published = []
    breaker = CircuitBreaker("p", 1, 30, 20, clock=lambda: now[0], on_local_open=published.append)
    breaker.adopt_open(5.0)  # another replica opened it 15 s ago
    assert breaker.current_state() == OPEN and published == []
    now[0] += 5.0
    assert breaker.current_state() == HALF_OPEN  # half-open when the shared cooldown ends
    breaker.record_failure()  # the probe fails here: this open IS local, so it is shared
    assert breaker.current_state() == OPEN and published == ["p"]


@pytest.mark.parametrize("remaining", [0.0, -1.0])
def test_expired_records_are_ignored(remaining):
    breaker = CircuitBreaker("p", 1, 30, 20)
    breaker.adopt_open(remaining)
    assert breaker.current_state() == CLOSED
