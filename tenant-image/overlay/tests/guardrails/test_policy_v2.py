"""Policy v2 integration through the REAL registry dispatch (review blockers 2, 3, 4, 5, 7, 8).

Fake tools are registered in the real registry under the ``browser`` toolset and dispatched via
``model_tools.handle_function_call`` with the plugin loaded by upstream's real discovery.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time

import psutil
import pytest

from squire_overlay import guardrails as G
from squire_overlay.guardrails import owned as OW
from squire_overlay.guardrails import policy as POL

ORIGIN_A = {"platform": "whatsapp", "chat_id": "111@s.whatsapp.net", "session_key": "agent:whatsapp:dm:111"}


@pytest.fixture
def fast_policy(monkeypatch):
    monkeypatch.setattr(POL, "_settings", lambda: dict(
        POL.DEFAULTS, lease_wait_s=5.0, startup_allowance_s=0.0, kill_grace_s=1.0, cleanup_reserve_s=2.0,
        resource_scope=False))
    monkeypatch.setattr(POL.preflight, "read_host", lambda: POL.preflight.Readings(0.1, 2, 4000, 0, 0))
    POL.reset_for_tests()
    yield
    OW.reset_for_tests()


@pytest.fixture
def fake_tools():
    from tools.registry import registry
    state = {"calls": 0, "hold": threading.Event(), "pids": [], "unrelated": []}

    def fake_browser_probe(args, **kw):
        state["calls"] += 1
        mode = args.get("mode", "ok")
        if mode == "hang_owned":
            proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(100)"],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            OW.current().spawned(proc)
            state["pids"].append(proc.pid)
            # an UNRELATED chrom-like child spawned mid-call by someone else in this process (e.g. PDF render)
            u = subprocess.Popen([sys.executable, "-c", "import time; n='chromium-pdf'; time.sleep(30)"],
                                 start_new_session=True)
            state["unrelated"].append(u.pid)
            proc.communicate()
            return json.dumps({"success": False, "error": "exec died"})
        if mode == "block":
            state["hold"].wait(timeout=20)
            return json.dumps({"success": True, "output": "released"})
        if mode == "auth":
            return json.dumps({"success": True, "output": "redirected to https://shop.example.com/signin"})
        return json.dumps({"success": True, "output": "ok"})

    schema = {"name": "fake_browser_probe", "description": "t", "parameters": {"type": "object", "properties": {}}}
    registry.register(name="fake_browser_probe", toolset="browser", schema=schema, handler=fake_browser_probe)
    yield state
    state["hold"].set()
    for p in state["unrelated"]:
        try:
            psutil.Process(p).kill()
        except Exception:
            pass
    try:
        registry.deregister("fake_browser_probe")
    except Exception:
        pass


def _call(name, args, *, task_id="root", session_id="s-root", tool_call_id="c"):
    from model_tools import handle_function_call
    return handle_function_call(name, args, task_id=task_id, session_id=session_id, tool_call_id=tool_call_id)


@pytest.fixture
def origin_a(monkeypatch):
    monkeypatch.setattr(POL, "_current_origin", lambda session_id="": dict(ORIGIN_A))


# --- blocker 3/4: ownership-exact kill ------------------------------------------------------

def test_deadline_kills_only_owned_group_unrelated_midcall_child_survives(loaded_plugins, fast_policy, fake_tools, origin_a):
    t0 = time.monotonic()
    out = json.loads(_call("fake_browser_probe", {"mode": "hang_owned", "timeout_s": 5}, tool_call_id="H"))
    assert out["guardrail"]["reason"] == "deadline_exceeded", out
    assert time.monotonic() - t0 < 12
    owned, unrelated = fake_tools["pids"][0], fake_tools["unrelated"][0]
    assert not psutil.pid_exists(owned) or psutil.Process(owned).status() == psutil.STATUS_ZOMBIE
    assert psutil.pid_exists(unrelated) and psutil.Process(unrelated).status() != psutil.STATUS_ZOMBIE
    assert out["guardrail"]["killed_processes"] == 1 and out["guardrail"]["worker_finished"] is True
    assert G.lease.holders("browser") == []


# --- blocker 2: one absolute deadline including queue ---------------------------------------

def test_queue_wait_counts_against_the_same_absolute_deadline(loaded_plugins, fast_policy, fake_tools, origin_a):
    results = {}

    def first():
        results["a"] = _call("fake_browser_probe", {"mode": "block", "timeout_s": 30}, task_id="a", session_id="s-a", tool_call_id="A")

    t = threading.Thread(target=first); t.start()
    time.sleep(0.3)
    t0 = time.monotonic()
    # timeout_s=5 (clamped min) and lease_wait_s=5 => the lease wait is capped by the deadline, not additive
    out = json.loads(_call("fake_browser_probe", {"mode": "ok", "timeout_s": 5}, task_id="b", session_id="s-b", tool_call_id="B"))
    el = time.monotonic() - t0
    assert out["guardrail"]["reason"] == "lease_wait_timeout", out
    assert el < 5.5, el
    assert out["guardrail"]["deadline_s"] == 5.0 and out["guardrail"]["waited_s"] <= 5.1
    fake_tools["hold"].set(); t.join(10)


# --- blocker 5: no trusted origin => no sharing ----------------------------------------------

def test_two_empty_session_callers_cannot_share_findings_or_ledger(loaded_plugins, fast_policy, fake_tools, monkeypatch):
    # No gateway contextvars bound, no hook-populated origin, empty session ids: must fail closed.
    monkeypatch.setattr(POL, "_session_env", lambda name: "")
    out1 = json.loads(_call("fake_browser_probe", {"mode": "auth", "code": "new_tab('https://shop.example.com/a')"},
                            task_id="", session_id="", tool_call_id="X1"))
    assert out1["success"] is True
    out2 = json.loads(_call("fake_browser_probe", {"mode": "ok", "code": "new_tab('https://shop.example.com/b')"},
                            task_id="", session_id="", tool_call_id="X2"))
    assert "findings" not in out2["guardrail"], out2   # the auth finding of caller 1 is NOT visible to caller 2
    assert out1["guardrail"]["origin_scope"] != out2["guardrail"]["origin_scope"]
    scopes = {p.stem for p in (G.lease.state_dir() / "findings").glob("*.json")}
    assert len(scopes) >= 2 and "session_anon" not in scopes
    ledgers = {p.stem for p in (G.lease.state_dir() / "ledger").glob("*.json")}
    assert len(ledgers) >= 2 and "session_anon" not in ledgers


def test_child_inherits_trusted_parent_origin_and_shares_findings(loaded_plugins, fast_policy, fake_tools, monkeypatch):
    from hermes_cli.plugins import invoke_hook
    monkeypatch.setattr(POL, "_session_env", lambda name: {"HERMES_SESSION_PLATFORM": "whatsapp",
                                                            "HERMES_SESSION_CHAT_ID": "111@s.whatsapp.net",
                                                            "HERMES_SESSION_KEY": "agent:whatsapp:dm:111"}.get(name, ""))
    invoke_hook("subagent_start", parent_session_id="s-parent", parent_turn_id="t", parent_subagent_id=None,
                child_session_id="s-kid", child_subagent_id="k", child_role="r", child_goal="g")
    monkeypatch.setattr(POL, "_session_env", lambda name: "")   # child thread without contextvars
    out1 = json.loads(_call("fake_browser_probe", {"mode": "auth", "code": "new_tab('https://shop.example.com/a')"},
                            task_id="s-kid", session_id="s-kid", tool_call_id="K1"))
    out2 = json.loads(_call("fake_browser_probe", {"mode": "ok", "code": "new_tab('https://shop.example.com/b')"},
                            task_id="s-kid2", session_id="s-kid", tool_call_id="K2"))
    assert out2["guardrail"]["findings"][0]["kind"] == "auth_required"
    assert out1["guardrail"]["origin_scope"] == out2["guardrail"]["origin_scope"] == G.ledger.task_key(ORIGIN_A)


# --- blocker 7: ledger exists from dispatch start, not first tool -----------------------------

def test_ledger_entry_exists_from_turn_start_before_any_tool(loaded_plugins, fast_policy, monkeypatch):
    from hermes_cli.plugins import invoke_hook
    monkeypatch.setattr(POL, "_session_env", lambda name: {"HERMES_SESSION_PLATFORM": "whatsapp",
                                                            "HERMES_SESSION_CHAT_ID": "111@s.whatsapp.net",
                                                            "HERMES_SESSION_KEY": "agent:whatsapp:dm:111"}.get(name, ""))
    invoke_hook("pre_llm_call", session_id="s-root", task_id="root", turn_id="t1", user_message="hi",
                conversation_history=[], is_first_turn=True, model="m", platform="whatsapp", parent_session_id="", sender_id="u")
    snap = G.ledger.Ledger().snapshot(ORIGIN_A)
    assert snap["status"] == "running" and snap["in_flight"] and list(snap["in_flight"].values())[0]["tool"] == "llm:turn"


# --- blocker 8: managed config reaches ctx.get_config through the real loader -----------------

def test_managed_guardrail_settings_are_effective_through_real_plugin_ctx(loaded_plugins, hermes_env):
    # user config tries to turn enforcement OFF; managed layer must win
    (hermes_env / "config.yaml").write_text(
        "plugins:\n  entries:\n    squire-overlay:\n      settings:\n        guardrails:\n          enforcement: false\n          browser_capacity: 9\n")
    from hermes_cli import managed_scope
    managed_scope.invalidate_managed_cache()
    import hermes_cli.config as cfg
    for name in ("invalidate_config_cache", "_invalidate_config_cache", "clear_config_cache"):
        fn = getattr(cfg, name, None)
        if callable(fn):
            fn(); break
    s = POL._settings()
    assert s["enforcement"] is True and s["browser_capacity"] == 1 and s["consecutive_limit"] == 2
    assert POL._ctx is not None and POL._ctx.get_config("guardrails")["enforcement"] is True
