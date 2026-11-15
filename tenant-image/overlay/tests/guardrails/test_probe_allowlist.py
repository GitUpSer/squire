"""Automatic browser-call dedupe is ABSENT by construction (review #2 blocker 1).

The earlier AST allowlist treated ``new_tab(<literal>)`` as read-only; a navigation is not read-only
(logout / approve / unsubscribe / magic-login / signed URLs) and two sessions carry different cookies.
Dedupe is deferred until the browser tool exposes explicit read-only semantics. These tests pin the
absence: no key for anything, no probe table, and — through the REAL middleware — two identical
navigations both execute (same code, same session; same code, different sessions).
"""

from __future__ import annotations

import json
import threading

import pytest

from squire_overlay.guardrails import findings as FD
from squire_overlay.guardrails import owned as OW
from squire_overlay.guardrails import policy as POL

ORIGIN = {"platform": "whatsapp", "chat_id": "111@s.whatsapp.net", "session_key": "agent:whatsapp:dm:111", "trusted": True}

CODES = [
    "new_tab('https://example.com')\nwait_for_load()\nprint(page_info())",
    "print(page_info())",
    "new_tab('https://x.com/logout')",
    "new_tab('https://x.com/a?X-Amz-Signature=1&X-Amz-Expires=60')",
    "goto_url('https://x.com/confirm?token=abc')",
    "js('document.title')",
]


@pytest.mark.parametrize("code", CODES)
def test_nothing_is_ever_dedupable(code):
    assert FD.NO_AUTOMATIC_DEDUPE is True
    assert FD.probe_key("browser_exec", {"code": code}) is None
    assert FD.probe_key("browser_exec", {"code": code, "session": "s1"}) is None


def test_no_probe_machinery_or_config_knob_left():
    for name in ("begin_probe", "wait_probe", "end_probe", "probe_done", "ProbeTicket", "is_readonly_probe",
                 "READONLY_PROBE_CALLS"):
        assert not hasattr(FD, name) and not hasattr(FD.Findings, name), name
    assert "probe_dedupe" not in POL.DEFAULTS
    src = open(POL.__file__).read()
    assert "begin_probe" not in src and "wait_probe" not in src and "probe_deduped" not in src


def test_findings_store_drops_legacy_probe_table(hermes_env):
    st = FD.Findings(scope="s")
    st.path.write_text(json.dumps({"findings": [], "probes": {"k": {"task_id": "t1", "result": "stale"}}}))
    assert st.all() == []
    assert "probes" not in json.loads(st.path.read_text())


@pytest.fixture
def fast_policy(monkeypatch):
    monkeypatch.setattr(POL, "_settings", lambda: dict(
        POL.DEFAULTS, lease_wait_s=10.0, startup_allowance_s=0.0, kill_grace_s=1.0, cleanup_reserve_s=2.0,
        resource_scope=False))
    monkeypatch.setattr(POL.preflight, "read_host", lambda: POL.preflight.Readings(0.1, 2, 4000, 0, 0))
    monkeypatch.setattr(POL, "_current_origin", lambda session_id="": dict(ORIGIN))
    POL.reset_for_tests()
    yield
    OW.reset_for_tests()


@pytest.fixture
def counting_tool():
    from tools.registry import registry
    state = {"calls": [], "lock": threading.Lock()}

    def fake_browser_nav(args, **kw):
        with state["lock"]:
            state["calls"].append((args.get("code"), args.get("session", "")))
        return json.dumps({"success": True, "output": f"ran {len(state['calls'])}"})

    schema = {"name": "fake_browser_nav", "description": "t", "parameters": {"type": "object", "properties": {}}}
    registry.register(name="fake_browser_nav", toolset="browser", schema=schema, handler=fake_browser_nav)
    yield state
    try:
        registry.deregister("fake_browser_nav")
    except Exception:
        pass


@pytest.mark.parametrize("sessions", [("same", "same"), ("cookies-a", "cookies-b")])
def test_real_middleware_runs_both_identical_navigations(loaded_plugins, fast_policy, counting_tool, sessions):
    from model_tools import handle_function_call
    code = "new_tab('https://x.com/logout')\nprint(page_info())"
    outs = {}

    def go(tag, session):
        outs[tag] = json.loads(handle_function_call(
            "fake_browser_nav", {"code": code, "session": session, "timeout_s": 5},
            task_id=f"t-{tag}", session_id=f"s-{tag}", tool_call_id=f"C-{tag}"))

    th = [threading.Thread(target=go, args=(f"n{i}", s)) for i, s in enumerate(sessions)]
    for t in th:
        t.start()
    for t in th:
        t.join(30)
    assert len(counting_tool["calls"]) == 2, counting_tool["calls"]
    assert all(o["success"] for o in outs.values()), outs
    assert all("deduped_from" not in o["guardrail"] and o["guardrail"]["reason"] == "ok" for o in outs.values()), outs
    assert {o["output"] for o in outs.values()} == {"ran 1", "ran 2"}, "each caller got ITS OWN run's output"
