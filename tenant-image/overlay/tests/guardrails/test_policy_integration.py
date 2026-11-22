"""Integration: guardrails attached through upstream's REAL plugin discovery + tool dispatch.

The fake tools below are registered in the real registry under the ``browser`` toolset and
dispatched via ``model_tools.handle_function_call`` — the same path the agent loop, the
concurrent executor, execute_code's RPC bridge and subagents all use — so what is proven here
is the production seam, not a mock of it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import psutil
import pytest

from squire_overlay import guardrails as G
from squire_overlay.guardrails import owned as OW
from squire_overlay.guardrails import policy as POL

ORIGIN = {"platform": "whatsapp", "chat_id": "111@s.whatsapp.net", "session_key": "agent:whatsapp:dm:111"}


@pytest.fixture
def fast_policy(monkeypatch):
    monkeypatch.setattr(POL, "_settings", lambda: dict(
        POL.DEFAULTS, lease_wait_s=0.3, startup_allowance_s=0.0, kill_grace_s=2.0, cleanup_reserve_s=3.0,
        resource_scope=False))
    monkeypatch.setattr(POL, "_current_origin", lambda session_id="": dict(ORIGIN))
    monkeypatch.setattr(POL.preflight, "read_host", lambda: POL.preflight.Readings(0.1, 2, 4000, 0, 0))
    POL.reset_for_tests()
    yield
    OW.reset_for_tests()


@pytest.fixture
def fake_tools():
    from tools.registry import registry

    state = {"calls": 0, "hold": threading.Event(), "pids": []}

    def fake_browser_probe(args, **kw):
        state["calls"] += 1
        mode = args.get("mode", "ok")
        if mode == "hang":
            proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(100)"],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            OW.current().spawned(proc)        # what the real spawn seam does for browser_exec
            state["pids"].append(proc.pid)
            proc.communicate()
            return json.dumps({"success": False, "error": "exec died"})
        if mode == "block":
            state["hold"].wait(timeout=10)
            return json.dumps({"success": True, "output": "released"})
        if mode == "infra":
            return json.dumps({"error": "browser-use exec timed out after 60s. The daemon may still be working"})
        if mode == "site404":
            return json.dumps({"success": True, "output": "HTTP 404 Not Found for https://retailer.example/p"})
        if mode == "auth":
            return json.dumps({"success": True, "output": "redirected to https://shop.example.com/signin"})
        return json.dumps({"success": True, "output": "ok " + str(args.get("code", ""))})

    schema = {"name": "fake_browser_probe", "description": "t", "parameters": {"type": "object", "properties": {}}}
    registry.register(name="fake_browser_probe", toolset="browser", schema=schema, handler=fake_browser_probe)
    registry.register(name="fake_light_tool", toolset="web", schema=dict(schema, name="fake_light_tool"),
                      handler=lambda args, **kw: json.dumps({"success": True, "output": "light"}))
    yield state
    state["hold"].set()
    for name in ("fake_browser_probe", "fake_light_tool"):
        try:
            registry.deregister(name)
        except Exception:
            pass


def _call(name, args, *, task_id="root", session_id="s-root", tool_call_id="c"):
    from model_tools import handle_function_call
    return handle_function_call(name, args, task_id=task_id, session_id=session_id, tool_call_id=tool_call_id)


def test_plugin_registers_tool_execution_middleware_and_hooks(loaded_plugins):
    loaded = loaded_plugins._plugins["squire-overlay"]
    assert loaded.error is None
    assert "tool_execution" in [m for m in loaded_plugins._middleware if loaded_plugins._middleware[m]]
    assert {"subagent_start", "subagent_stop", "on_session_end"} <= set(loaded.hooks_registered)
    assert "guardrails" in loaded.commands_registered


def test_legitimate_browser_work_passes_and_is_ledgered(loaded_plugins, fast_policy, fake_tools, hermes_env):
    out = json.loads(_call("fake_browser_probe", {"code": "print(1)", "timeout_s": 5}))
    assert out["success"] is True and out["output"] == "ok print(1)"
    assert "guardrail" in out and out["guardrail"]["reason"] == "ok"
    snap = G.ledger.Ledger().snapshot(ORIGIN)
    assert snap["counters"]["tool_calls"] == 1 and snap["in_flight"] == {}
    tel = snap["telemetry"][-1]
    assert {"queue_s", "preflight_s", "execution_s", "cleanup_s", "finalization_s", "total_s"} <= set(tel)


def test_lightweight_tool_is_not_leased_but_still_ledgered(loaded_plugins, fast_policy, fake_tools):
    held = G.lease.acquire("browser", owner="someone-else", wait_s=0)
    try:
        out = json.loads(_call("fake_light_tool", {}, tool_call_id="L"))
        assert out["success"] is True and "guardrail" not in out
    finally:
        held.release()
    assert G.ledger.Ledger().snapshot(ORIGIN)["counters"]["tool_calls"] == 1


def test_second_browser_caller_from_other_session_name_waits_then_gets_structured_refusal(loaded_plugins, fast_policy, fake_tools):
    results = {}

    def first():
        results["a"] = _call("fake_browser_probe", {"mode": "block", "session": "samsung", "timeout_s": 20},
                             task_id="a", session_id="s-a", tool_call_id="A")

    t = threading.Thread(target=first); t.start()
    time.sleep(0.3)
    out = json.loads(_call("fake_browser_probe", {"mode": "ok", "session": "pixel", "timeout_s": 20},
                           task_id="b", session_id="s-b", tool_call_id="B"))
    assert out["error"].startswith("browser lease unavailable")
    assert out["guardrail"]["reason"] == "lease_wait_timeout" and out["guardrail"]["holder"] == "s-a"
    assert fake_tools["calls"] == 1                   # the second handler never ran
    fake_tools["hold"].set(); t.join(timeout=5)
    assert json.loads(results["a"])["success"] is True


def test_hung_browser_worker_is_killed_within_budget_and_lease_released(loaded_plugins, fast_policy, fake_tools):
    t0 = time.monotonic()
    out = json.loads(_call("fake_browser_probe", {"mode": "hang", "timeout_s": 1}, tool_call_id="H"))
    elapsed = time.monotonic() - t0
    assert out["guardrail"]["reason"] == "deadline_exceeded", out
    assert elapsed < 15, elapsed
    pid = fake_tools["pids"][0]
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    assert out["guardrail"]["killed_processes"] == 1
    assert G.lease.holders("browser") == []           # released only after the worker returned
    # and it counted as ONE infra failure for this task's breaker
    assert json.loads(_call("fake_browser_probe", {"mode": "ok"}, tool_call_id="H2"))["success"] is True
    assert G.breaker.snapshot("browser")["tasks"][G.ledger.task_key(ORIGIN)]["consecutive"] == 0


def test_two_infra_failures_open_breaker_and_block_siblings_without_running_handler(loaded_plugins, fast_policy, fake_tools):
    for i in range(2):
        out = json.loads(_call("fake_browser_probe", {"mode": "infra"}, tool_call_id=f"I{i}", task_id="samsung", session_id="s-samsung"))
        assert out["guardrail"]["infra_reason"] == "infra_timeout"
    assert fake_tools["calls"] == 2
    out = json.loads(_call("fake_browser_probe", {"mode": "ok"}, tool_call_id="I3", task_id="pixel", session_id="s-pixel"))
    assert out["guardrail"]["reason"] == "breaker_open_consecutive_failures"
    assert fake_tools["calls"] == 2                   # not dispatched
    assert "retry" in out["error"].lower()


def test_site_404_and_auth_redirect_are_not_infra(loaded_plugins, fast_policy, fake_tools):
    for i in range(3):
        out = json.loads(_call("fake_browser_probe", {"mode": "site404"}, tool_call_id=f"N{i}"))
        assert out["success"] is True and out["guardrail"]["reason"] == "ok"
    out = json.loads(_call("fake_browser_probe", {"mode": "auth", "code": "new_tab('https://shop.example.com/p')"}, tool_call_id="AU"))
    assert out["success"] is True
    assert G.breaker.snapshot("browser")["tasks"].get(G.ledger.task_key(ORIGIN), {}).get("consecutive", 0) == 0
    f = G.findings.Findings(scope=G.ledger.task_key(ORIGIN)).lookup("shop.example.com")
    assert f and f[0]["kind"] == "auth_required"
    # sibling asking about the same host is told up-front (no browser launch) and how to override
    calls = fake_tools["calls"]
    out2 = json.loads(_call("fake_browser_probe", {"mode": "ok", "code": "new_tab('https://shop.example.com/q')"}, tool_call_id="AU2", task_id="pixel"))
    assert out2["guardrail"]["reason"] == "finding_known" and out2["guardrail"]["findings"][0]["kind"] == "auth_required"
    assert fake_tools["calls"] == calls and "# guardrails: ignore-findings" in out2["error"]
    assert G.ledger.Ledger().snapshot(ORIGIN)["in_flight"] == {}
    # the documented override is a comment INSIDE code (browser_exec's schema has no extra argument)
    out3 = json.loads(_call("fake_browser_probe", {"mode": "ok", "code": "# guardrails: ignore-findings\nnew_tab('https://shop.example.com/q')"},
                            tool_call_id="AU3", task_id="pixel"))
    assert out3["success"] is True and fake_tools["calls"] == calls + 1


def test_syntax_error_rejected_before_handler(loaded_plugins, fast_policy, fake_tools):
    out = json.loads(_call("browser_exec", {"code": "def x(:\n pass"}, tool_call_id="SY"))
    assert out["guardrail"]["reason"] == "code_syntax_error"
    assert fake_tools["calls"] == 0


def test_host_pressure_refuses_launch(loaded_plugins, fast_policy, fake_tools, monkeypatch):
    monkeypatch.setattr(POL.preflight, "read_host", lambda: POL.preflight.Readings(22.0, 2, 4000, 0, 0))
    out = json.loads(_call("fake_browser_probe", {"mode": "ok"}, tool_call_id="HP"))
    assert out["guardrail"]["reason"] == "host_pressure_load" and fake_tools["calls"] == 0


def test_child_lifecycle_hooks_feed_the_origin_ledger(loaded_plugins, fast_policy):
    from hermes_cli.plugins import invoke_hook
    invoke_hook("subagent_start", parent_session_id="s-root", parent_turn_id="t", parent_subagent_id=None,
                child_session_id="s-kid", child_subagent_id="k", child_role="research", child_goal="Samsung price")
    snap = G.ledger.Ledger().snapshot(ORIGIN)
    assert snap["children"]["s-kid"]["goal"] == "Samsung price"
    invoke_hook("subagent_stop", parent_session_id="s-root", parent_turn_id="t", child_session_id="s-kid",
                child_role="research", child_summary="done", child_status="completed", tool_call_history=[], duration_ms=10)
    assert G.ledger.Ledger().snapshot(ORIGIN)["children"]["s-kid"]["status"] == "completed"
    invoke_hook("on_session_end", session_id="s-root", task_id="root", turn_id="t", completed=True, failed=False,
                interrupted=False, turn_exit_reason="done", model="m", platform="whatsapp")
    assert G.ledger.Ledger().snapshot(ORIGIN)["status"] == "completed"


def test_no_secrets_in_guardrail_state(loaded_plugins, fast_policy, fake_tools, hermes_env):
    _call("fake_browser_probe", {"mode": "infra", "code": "fill = 'Authorization: Bearer sk-SECRET-XYZ'"}, tool_call_id="S1")
    blob = ""
    for p in (hermes_env / "guardrails").rglob("*.json"):
        blob += p.read_text()
    assert "sk-SECRET-XYZ" not in blob


def test_status_command_reports_effective_policy_and_reason_codes(loaded_plugins, fast_policy, fake_tools):
    _call("fake_browser_probe", {"mode": "infra"}, tool_call_id="ST")
    text = POL.status_text()
    assert "effective policy" in text.lower()
    assert "browser_capacity=1" in text and "consecutive_limit=2" in text and "recovery_budget_s=120" in text
    assert "infra_timeout" in text and "enforcement: on" in text.lower()


def test_every_refusal_closes_the_ledger_entry(loaded_plugins, fast_policy, fake_tools, monkeypatch):
    """Refused calls (breaker / preflight / lease / syntax / finding) must not stay in_flight: the
    notifier would otherwise report a phantom running tool until the turn ends."""
    key = G.ledger.task_key(ORIGIN)
    # breaker
    for i in range(2):
        _call("fake_browser_probe", {"mode": "infra"}, tool_call_id=f"R{i}")
    out = json.loads(_call("fake_browser_probe", {"mode": "ok"}, tool_call_id="R-breaker"))
    assert out["guardrail"]["reason"] == "breaker_open_consecutive_failures"
    snap = G.ledger.Ledger().snapshot(ORIGIN)
    assert snap["in_flight"] == {} and snap["blockers"]["root"]["code"] == "breaker_open_consecutive_failures"
    ends = [e for e in snap["events"] if e["kind"] == "tool_end" and e["tool_call_id"] == "R-breaker"]
    assert ends and ends[0]["status"] == "refused" and ends[0]["reason"] == "breaker_open_consecutive_failures"
    G.breaker.clear_task_state()
    # preflight
    monkeypatch.setattr(POL.preflight, "read_host", lambda: POL.preflight.Readings(22.0, 2, 4000, 0, 0))
    out = json.loads(_call("fake_browser_probe", {"mode": "ok"}, tool_call_id="R-pre"))
    assert out["guardrail"]["reason"] == "host_pressure_load"
    assert G.ledger.Ledger().snapshot(ORIGIN)["in_flight"] == {}
    monkeypatch.setattr(POL.preflight, "read_host", lambda: POL.preflight.Readings(0.1, 2, 4000, 0, 0))
    # syntax
    out = json.loads(_call("browser_exec", {"code": "def x(:\n pass"}, tool_call_id="R-syn"))
    assert out["guardrail"]["reason"] == "code_syntax_error"
    assert G.ledger.Ledger().snapshot(ORIGIN)["in_flight"] == {}
    # lease (held by someone else)
    held = G.lease.acquire("browser", owner="other", wait_s=0)
    try:
        out = json.loads(_call("fake_browser_probe", {"mode": "ok"}, tool_call_id="R-lease"))
        assert out["guardrail"]["reason"] == "lease_wait_timeout"
        assert G.ledger.Ledger().snapshot(ORIGIN)["in_flight"] == {}
    finally:
        held.release()


def test_lease_is_released_when_the_wrapper_raises_after_acquire(loaded_plugins, fast_policy, fake_tools, monkeypatch):
    """Anything raised between lease acquisition and the finalizer taking over (Thread.start()
    RuntimeError, runtime-dir OSError…) is swallowed by the middleware; the flock must not leak."""
    def boom(*a, **kw):
        raise RuntimeError("can't start new thread")
    monkeypatch.setattr(POL.DL, "run_with_deadline", boom)
    out = json.loads(_call("fake_browser_probe", {"mode": "ok"}, tool_call_id="RAISE"))
    assert out["guardrail"]["reason"] == "guardrail_error"
    assert G.lease.holders("browser") == [], "lease must be released on the exception path"
    assert G.ledger.Ledger().snapshot(ORIGIN)["in_flight"] == {}
    assert OW.live_work() == []
    monkeypatch.undo()
    # and the next call is admitted normally
    assert json.loads(_call("fake_browser_probe", {"mode": "ok"}, tool_call_id="AFTER"))["success"] is True


def test_refused_call_gives_back_the_half_open_probe_slot(loaded_plugins, fast_policy, fake_tools, monkeypatch):
    key = G.ledger.task_key(ORIGIN)
    for i in range(2):   # trip the host circuit from this task
        _call("fake_browser_probe", {"mode": "infra"}, tool_call_id=f"P{i}")
    with G.breaker._Store("browser").locked() as d:   # cooldown elapsed; this task's own streak cleared
        d["host"]["open_until"] = time.time() - 1
        d["tasks"] = {}
    monkeypatch.setattr(POL.preflight, "read_host", lambda: POL.preflight.Readings(22.0, 2, 4000, 0, 0))
    out = json.loads(_call("fake_browser_probe", {"mode": "ok"}, tool_call_id="P-pre"))
    assert out["guardrail"]["reason"] == "host_pressure_load"
    host = G.breaker.snapshot("browser")["host"]
    assert host["state"] == "half_open" and host["probe"] is None, host
    monkeypatch.setattr(POL.preflight, "read_host", lambda: POL.preflight.Readings(0.1, 2, 4000, 0, 0))
    out = json.loads(_call("fake_browser_probe", {"mode": "ok"}, tool_call_id="P-probe", task_id="sibling", session_id="s-sib"))
    assert out["success"] is True, "the next caller is admitted as the probe, not refused as probe_in_flight"
    assert G.breaker.snapshot("browser")["host"]["state"] == "closed"


def test_recover_clears_breaker_state_for_a_locked_out_chat(loaded_plugins, fast_policy, fake_tools):
    for i in range(2):
        _call("fake_browser_probe", {"mode": "infra"}, tool_call_id=f"L{i}")
    assert json.loads(_call("fake_browser_probe", {"mode": "ok"}, tool_call_id="L-x"))["guardrail"]["reason"] == "breaker_open_consecutive_failures"
    txt = POL.recover_text()
    assert "breaker: cleared browser" in txt, txt
    assert json.loads(_call("fake_browser_probe", {"mode": "ok"}, tool_call_id="L-y"))["success"] is True
