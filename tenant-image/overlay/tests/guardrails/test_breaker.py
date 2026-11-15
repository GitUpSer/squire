"""Infrastructure circuit breaker (acceptance item 3).

Persisted per dependency in HERMES_HOME/guardrails so sibling session names / processes share it.
Fake clock everywhere; no sleeps.
"""

from __future__ import annotations

import json

import pytest

from squire_overlay.guardrails import breaker as B


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


def test_classify_separates_infra_from_normal_errors():
    assert B.classify('{"error": "browser-use exec timed out after 60s. The daemon may still be working"}') == "infra_timeout"
    assert B.classify('{"error": "Failed to launch browser-use CLI: [Errno 2]"}') == "infra_launch_failed"
    assert B.classify('{"error": "CDP connection refused: ECONNREFUSED 127.0.0.1:9222"}') == "infra_cdp_unreachable"
    assert B.classify('{"error": "browser-use CLI not found on PATH"}') == "infra_cli_missing"
    # NOT infrastructure: site/validation/auth outcomes
    assert B.classify('{"success": true, "output": "HTTP 404 Not Found for https://retailer.example/x"}') is None
    assert B.classify('{"success": false, "output": "Traceback... NoSuchElementError: #price"}') is None
    assert B.classify('{"error": "Invalid session name \'bad name\'"}') is None
    assert B.classify('{"success": false, "output": "Login required: redirected to /signin"}') is None
    assert B.classify('{"error": "No code provided."}') is None


def test_opens_after_two_consecutive_infra_failures_per_task(hermes_env, clock):
    br = B.Breaker("browser", task_id="t1", now=clock, recovery_budget_s=120)
    assert br.check().allowed
    br.record_failure("infra_timeout", cost_s=30)
    assert br.check().allowed
    br.record_failure("infra_timeout", cost_s=30)
    v = br.check()
    assert not v.allowed and v.reason == "breaker_open_consecutive_failures" and v.failures == 2


def test_opens_when_cumulative_recovery_budget_exhausted(hermes_env, clock):
    # budget is cumulative over one failure STREAK (no success in between); consecutive_limit raised
    # so the budget — not the streak counter — is what opens the circuit here
    br = B.Breaker("browser", task_id="t2", now=clock, recovery_budget_s=120, consecutive_limit=5)
    br.record_failure("infra_timeout", cost_s=100)
    clock.t += 60
    br.record_failure("infra_cdp_unreachable", cost_s=30)
    v = br.check()
    assert not v.allowed and v.reason == "breaker_open_recovery_budget" and v.spent_s == 130
    assert 0 < v.resets_in_s <= B.DEFAULT_RESET_AFTER_S, "the refusal tells the caller when the state heals"


def test_success_resets_consecutive_and_budget(hermes_env, clock):
    """The task key is the chat's stable session key: a demonstrated working dependency must start
    the task over, otherwise one bad streak locks the chat out for the rest of its life."""
    br = B.Breaker("browser", task_id="t3", now=clock, recovery_budget_s=120, consecutive_limit=5)
    br.record_failure("infra_timeout", cost_s=100)
    br.record_success()
    snap = B.snapshot("browser")["tasks"]["t3"]
    assert snap["consecutive"] == 0 and snap["spent_s"] == 0.0
    br.record_failure("infra_timeout", cost_s=100)
    assert br.check().allowed, "a new streak gets a fresh budget"


def test_task_state_is_forgotten_after_the_rolling_window(hermes_env, clock):
    br = B.Breaker("browser", task_id="t-win", now=clock, reset_after_s=600)
    br.record_failure("infra_timeout", cost_s=30)
    br.record_failure("infra_timeout", cost_s=30)
    assert not br.check().allowed
    clock.t += 601                      # window elapsed since the LAST failure
    v = br.check()
    assert v.allowed and v.failures == 0 and v.spent_s == 0
    # an old streak never carries into a new failure after the window either
    br.record_failure("infra_timeout", cost_s=10)
    assert B.snapshot("browser")["tasks"]["t-win"]["consecutive"] == 1


def test_window_is_measured_from_the_last_failure_not_the_first(hermes_env, clock):
    br = B.Breaker("browser", task_id="t-win2", now=clock, reset_after_s=600)
    br.record_failure("infra_timeout", cost_s=30)
    clock.t += 500
    br.record_failure("infra_timeout", cost_s=30)   # streak continues → open
    clock.t += 200                                   # 700 s after the first, 200 s after the last
    assert not br.check().allowed


def test_release_probe_gives_the_half_open_slot_back(hermes_env, clock):
    """A caller admitted as the half-open probe that is then refused by preflight/lease must not
    occupy the slot for PROBE_TTL_S (siblings would see dependency_half_open_probe_in_flight)."""
    a = B.Breaker("browser", task_id="a", now=clock)
    a.record_failure("infra_timeout", cost_s=1); a.record_failure("infra_timeout", cost_s=1)   # trips host
    clock.t += 61
    b = B.Breaker("browser", task_id="b", now=clock)
    assert b.check().half_open is True
    c = B.Breaker("browser", task_id="c", now=clock)
    assert c.check().reason == "dependency_half_open_probe_in_flight"
    assert b.release_probe() is True
    assert c.check().half_open is True, "the slot is free again immediately"
    assert c.release_probe() is True and b.release_probe() is False


def test_clear_task_state_resets_tasks_and_closes_host(hermes_env, clock):
    a = B.Breaker("browser", task_id="a", now=clock)
    a.record_failure("infra_timeout", cost_s=1); a.record_failure("infra_timeout", cost_s=1)
    assert not a.check().allowed and B.snapshot("browser")["host"]["state"] == "open"
    cleared = B.clear_task_state()
    assert cleared == {"browser": 1}
    assert a.check().allowed and B.snapshot("browser")["host"]["state"] == "closed"


def test_shared_host_health_blocks_sibling_tasks_and_half_open_probe(hermes_env, clock):
    """Two tasks (different session names) hit the same dependency; the 2nd task is stopped
    by host health, not by its own counters. After cooldown, exactly ONE probe is admitted."""
    a = B.Breaker("browser", task_id="samsung", now=clock)
    a.record_failure("infra_timeout", cost_s=60)
    a.record_failure("infra_timeout", cost_s=60)
    b = B.Breaker("browser", task_id="pixel", now=clock)
    v = b.check()
    assert not v.allowed and v.reason == "dependency_unhealthy_host" and v.retry_after_s > 0
    clock.t += v.retry_after_s + 1
    p1 = b.check()
    assert p1.allowed and p1.half_open is True
    c = B.Breaker("browser", task_id="retailers", now=clock)
    p2 = c.check()
    assert not p2.allowed and p2.reason == "dependency_half_open_probe_in_flight"
    b.record_success()           # probe succeeded → host health closes
    assert c.check().allowed and not c.check().half_open


def test_half_open_probe_failure_reopens_with_longer_backoff(hermes_env, clock):
    a = B.Breaker("browser", task_id="a", now=clock)
    a.record_failure("infra_timeout", cost_s=1); a.record_failure("infra_timeout", cost_s=1)
    first = B.Breaker("browser", task_id="b", now=clock).check().retry_after_s
    clock.t += first + 1
    b = B.Breaker("browser", task_id="b", now=clock)
    assert b.check().half_open
    b.record_failure("infra_timeout", cost_s=1)
    second = B.Breaker("browser", task_id="c", now=clock).check().retry_after_s
    assert second > first


def test_state_is_persisted_and_credential_safe(hermes_env, clock):
    br = B.Breaker("browser", task_id="t9", now=clock)
    br.record_failure("infra_timeout", cost_s=5, detail="Authorization: Bearer sk-SECRET123 timed out")
    raw = (hermes_env / "guardrails" / "breaker" / "browser.json").read_text()
    assert "sk-SECRET123" not in raw and "Bearer" not in raw
    assert json.loads(raw)["tasks"]["t9"]["consecutive"] == 1


def test_uncertain_writes_are_never_auto_retry_candidates():
    assert B.retry_policy("browser_exec", {"code": "js(\"document.querySelector('#buy').click()\")"}, "infra_timeout") == "no_retry_uncertain_write"
    assert B.retry_policy("browser_vault_fill", {}, "infra_timeout") == "no_retry_auth"
    assert B.retry_policy("browser_exec", {"code": "print(page_info())"}, "infra_timeout") == "retry_allowed"
    assert B.retry_policy("browser_exec", {"code": "print(page_info())"}, None) == "not_infra"
