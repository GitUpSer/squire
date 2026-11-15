"""END-TO-END through upstream's REAL ``browser_exec`` (not a fake tool): plugin loaded by real
discovery, dispatch via ``model_tools.handle_function_call``, the ownership seam installed in
``tools.browser_use_cli``, and a fake ``browser-use`` binary on a private PATH that behaves like the
real CLI's worst case — it detaches a harness-like daemon (reads BH_RUNTIME_DIR, publishes bu.pid /
bu.sock, keeps writing a marker) and then hangs.

Proves (review blocker 4): after the deadline the CLI group is dead, the detached daemon is STOPPED
(marker stops), the lease is free, and the user's own pre-existing daemon-like process is untouched.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from squire_overlay import guardrails as G
from squire_overlay.guardrails import owned as OW
from squire_overlay.guardrails import policy as POL

from .fake_harness import FAKE_CLI

ORIGIN = {"platform": "whatsapp", "chat_id": "111@s.whatsapp.net", "session_key": "agent:whatsapp:dm:111", "trusted": True}


@pytest.fixture
def fake_browser_use(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    script = bin_dir / "browser-use"
    script.write_text(f"#!{sys.executable}\nimport sys\nsys.argv = ['browser-use']\n{FAKE_CLI}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    from tools import browser_use_cli as BU
    monkeypatch.setattr(BU, "_find_cli", lambda: [str(script)])
    monkeypatch.setattr(BU, "_route_backend", lambda env, session, task_id, local: None)
    monkeypatch.setattr(BU, "_attach_vault_supervisor", lambda env, task_id: None)
    return script


@pytest.fixture
def fast_policy(monkeypatch):
    monkeypatch.setattr(POL, "_settings", lambda: dict(
        POL.DEFAULTS, lease_wait_s=2.0, startup_allowance_s=0.0, kill_grace_s=1.0, cleanup_reserve_s=3.0,
        resource_scope=False))
    monkeypatch.setattr(POL, "_current_origin", lambda session_id="": dict(ORIGIN))
    monkeypatch.setattr(POL.preflight, "read_host", lambda: POL.preflight.Readings(0.1, 2, 4000, 0, 0))
    POL.reset_for_tests()
    yield
    OW.reset_for_tests()


def test_real_browser_exec_hang_kills_cli_and_stops_detached_daemon(loaded_plugins, fast_policy, fake_browser_use, tmp_path):
    from model_tools import handle_function_call
    assert OW.seam_installed(), "plugin load must install the ownership seam into tools.browser_use_cli"
    pre = subprocess.Popen([sys.executable, "-c", "import time; d='browser_harness.daemon-lookalike'; time.sleep(30)"],
                           start_new_session=True)
    try:
        t0 = time.monotonic()
        raw = handle_function_call("browser_exec", {"code": "print('hi')", "timeout_s": 5},
                                   task_id="root", session_id="s-e2e", tool_call_id="E2E")
        el = time.monotonic() - t0
        out = json.loads(raw)
        assert out["guardrail"]["reason"] == "deadline_exceeded", out
        assert el < 12, el
        owned = out["guardrail"]["owned"]
        assert owned["pgids"] and owned["daemon"] is True, owned
        rt = OW.runtime_dir_for(f"{G.ledger.task_key(ORIGIN)}:s-e2e")
        marker = Path(rt) / "marker"
        assert marker.exists()
        s1 = marker.stat().st_size; time.sleep(0.4); s2 = marker.stat().st_size
        assert s1 == s2, "detached daemon kept running after the CLI died"
        for pg in owned["pgids"]:
            assert not psutil.pid_exists(pg) or psutil.Process(pg).status() == psutil.STATUS_ZOMBIE
        assert G.lease.holders("browser") == []
        assert psutil.pid_exists(pre.pid) and psutil.Process(pre.pid).status() != psutil.STATUS_ZOMBIE
        # the next call of the same session is admitted again (lease free, breaker counts ONE failure)
        raw2 = handle_function_call("browser_exec", {"code": "print('hi')", "timeout_s": 5},
                                    task_id="root", session_id="s-e2e", tool_call_id="E2E2")
        assert json.loads(raw2)["guardrail"]["reason"] in ("deadline_exceeded",)
    finally:
        pre.kill(); pre.wait()
