import threading

import pytest
from core.budget import BudgetExceeded, RequestBudget, from_config


def test_reserve_counts_what_it_claims():
    budget = RequestBudget(max_requests=5)

    budget.reserve()
    budget.reserve(2)

    assert budget.spent() == 3
    assert budget.remaining() == 2


def test_reserve_raises_at_the_cap_and_not_before():
    budget = RequestBudget(max_requests=3)

    budget.reserve(3)

    # The cap is reached, not exceeded, so nothing has raised yet.
    assert budget.spent() == 3
    with pytest.raises(BudgetExceeded, match="3 of 3 requests spent"):
        budget.reserve()


def test_a_reservation_larger_than_the_remainder_raises_before_spending():
    budget = RequestBudget(max_requests=5)
    budget.reserve(4)

    with pytest.raises(BudgetExceeded, match="2 more would exceed"):
        budget.reserve(2)

    # Nothing was spent by the failed reservation.
    assert budget.spent() == 4


def test_no_cap_never_raises():
    budget = RequestBudget(max_requests=None)

    for _ in range(50):
        budget.reserve()

    assert budget.spent() == 50
    assert budget.remaining() is None


def test_zero_or_negative_reservation_is_a_bug():
    with pytest.raises(ValueError, match="cannot reserve 0"):
        RequestBudget().reserve(0)


def test_rpm_paces_without_sleeping_in_the_test(monkeypatch):
    # A fake clock proves the wait is computed without the test waiting for it.
    now = [1000.0]
    slept: list[float] = []
    monkeypatch.setattr("core.budget.time.monotonic", lambda: now[0])

    def fake_sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    monkeypatch.setattr("core.budget.time.sleep", fake_sleep)
    budget = RequestBudget(rpm=2)

    budget.reserve()
    budget.reserve()
    assert slept == []

    # The third call must wait for the first to leave the 60s window.
    budget.reserve()
    assert slept == [pytest.approx(60.0)]
    assert budget.spent() == 3


def test_rpm_does_not_wait_once_the_window_has_moved_on(monkeypatch):
    now = [1000.0]
    slept: list[float] = []
    monkeypatch.setattr("core.budget.time.monotonic", lambda: now[0])
    monkeypatch.setattr("core.budget.time.sleep", lambda s: slept.append(s))
    budget = RequestBudget(rpm=1)

    budget.reserve()
    now[0] += 61.0
    budget.reserve()

    assert slept == []


def test_rpm_raises_rather_than_sleeping_past_the_cap(monkeypatch):
    monkeypatch.setattr("core.budget.time.sleep", lambda s: pytest.fail("should not sleep"))
    budget = RequestBudget(max_requests=1, rpm=1)

    budget.reserve()

    # The cap is checked before the rate window, so this fails fast instead of
    # blocking for a minute and then failing.
    with pytest.raises(BudgetExceeded):
        budget.reserve()


def test_concurrent_reserve_never_oversubscribes():
    budget = RequestBudget(max_requests=20)
    granted, refused = [], []

    def worker():
        try:
            budget.reserve()
            granted.append(1)
        except BudgetExceeded:
            refused.append(1)

    threads = [threading.Thread(target=worker) for _ in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(granted) == 20
    assert len(refused) == 20
    assert budget.spent() == 20


def test_from_config_reads_the_run_block():
    budget = from_config({"request_budget": 200, "requests_per_minute": 200})

    assert (budget.max_requests, budget.rpm) == (200, 200)
    assert from_config({}).max_requests is None


def test_from_config_reads_the_committed_key_name():
    # The key the config actually carries is `rpm`. Reading only
    # `requests_per_minute` left the budget unpaced while every key check
    # still passed, because a missing rpm looks exactly like "no pacing".
    from core.budget import from_config

    budget = from_config({"request_budget": 10000, "rpm": 180})

    assert budget.max_requests == 10000
    assert budget.rpm == 180


def test_from_config_accepts_the_alias():
    from core.budget import from_config

    assert from_config({"requests_per_minute": 60}).rpm == 60


def test_from_config_against_the_real_evals_config():
    # Guards the cross-file contract: if configs/evals.yaml renames either
    # key, this fails in `make check` rather than during a live run.
    from core.budget import from_config
    from core.config import load_yaml
    from core.paths import CONFIGS

    budget = from_config(load_yaml(CONFIGS / "evals.yaml")["run"])

    assert budget.max_requests is not None, "run.request_budget missing from evals.yaml"
    assert budget.rpm is not None, "run.rpm missing from evals.yaml"
