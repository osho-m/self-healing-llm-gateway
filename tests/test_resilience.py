from app.resilience import CLOSED, HALF_OPEN, OPEN, CircuitBreaker, HealthTracker


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def make(clock, changes=None):
    return CircuitBreaker(
        "p",
        threshold=3,
        window_s=10,
        cooldown_s=5,
        on_change=lambda n, a, b: changes.append((a, b)) if changes is not None else None,
        clock=clock,
    )


def test_opens_after_threshold_failures_within_window():
    clock, changes = Clock(), []
    cb = make(clock, changes)
    for _ in range(3):
        assert cb.allow()
        cb.record_failure()
    assert cb.state == OPEN
    assert not cb.allow()
    assert changes == [(CLOSED, OPEN)]


def test_failures_outside_window_do_not_open():
    clock = Clock()
    cb = make(clock)
    cb.record_failure()
    cb.record_failure()
    clock.t = 11
    cb.record_failure()
    assert cb.state == CLOSED


def test_half_open_admits_exactly_one_probe_and_success_closes():
    clock, changes = Clock(), []
    cb = make(clock, changes)
    for _ in range(3):
        cb.record_failure()
    clock.t = 5
    assert cb.current_state() == HALF_OPEN
    assert cb.allow() is True
    assert cb.allow() is False  # second concurrent caller rejected
    cb.record_success()
    assert cb.state == CLOSED and cb.allow()
    assert changes == [(CLOSED, OPEN), (OPEN, HALF_OPEN), (HALF_OPEN, CLOSED)]


def test_half_open_failure_reopens_and_restarts_cooldown():
    clock = Clock()
    cb = make(clock)
    for _ in range(3):
        cb.record_failure()
    clock.t = 5
    assert cb.allow()
    cb.record_failure()
    assert cb.state == OPEN
    clock.t = 9
    assert cb.current_state() == OPEN
    clock.t = 10
    assert cb.current_state() == HALF_OPEN


def test_release_frees_probe_slot():
    clock = Clock()
    cb = make(clock)
    for _ in range(3):
        cb.record_failure()
    clock.t = 5
    assert cb.allow()
    cb.release()
    assert cb.allow()


def test_health_tracker_statuses():
    h = HealthTracker("p", window_s=60)
    for _ in range(10):
        h.record(True, 100, now=None)
    assert h.evaluate(0.05, 0.5, 5000, CLOSED) == "healthy"
    h.record(False, 5, "boom")
    assert h.evaluate(0.05, 0.5, 5000, CLOSED) == "degraded"
    assert h.evaluate(0.05, 0.5, 5000, OPEN) == "down"
    assert [s for _, s, _ in h.history] == ["degraded", "down"]
    assert h.stats()["p99_ms"] == 100
