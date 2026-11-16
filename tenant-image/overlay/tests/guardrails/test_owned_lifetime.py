"""Owned daemon lifetime = the session/turn that owns it (review blocker 4 / orphan hygiene).

Normal completion keeps the owned harness daemon for the next call of the same session (no cold
start per call); the turn-end hook stops it with verified ownership, so nothing is left behind.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

from squire_overlay.guardrails import deadline as D
from squire_overlay.guardrails import owned as OW
from squire_overlay.guardrails import policy as POL
from .fake_harness import FAKE_DAEMON


def _daemon_spawning_call(rt: str):
    def call(args):
        p = subprocess.Popen([sys.executable, "-c", FAKE_DAEMON], env={"BH_RUNTIME_DIR": rt, "HERMES_GUARDRAIL_OWNER": OW._owner_marker(rt),
                                                                       "PATH": "/usr/bin:/bin"},
                             start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if (Path(rt) / "bu.pid").exists():
                break
            time.sleep(0.05)
        return '{"success": true}'
    return call


def test_normal_completion_keeps_owned_daemon_and_session_end_stops_it(hermes_env, monkeypatch):
    rt = OW.runtime_dir_for("agent_whatsapp_dm_111:s-1")
    work = OW.OwnedWork(key="c1", runtime_dir=rt)
    out = D.run_with_deadline(_daemon_spawning_call(rt), {}, deadline_at=time.monotonic() + 10, work=work,
                              finalize=lambda: None)
    assert out.reason == "ok" and out.finalized is True
    dpid = int((Path(rt) / "bu.pid").read_text())
    assert psutil.pid_exists(dpid) and OW.daemon_is_ours(dpid, rt), "daemon must survive a normal call"
    try:
        monkeypatch.setattr(POL, "_current_origin", lambda session_id="": {"platform": "whatsapp", "chat_id": "111@s.whatsapp.net",
                                                                            "session_key": "agent:whatsapp:dm:111", "trusted": True})
        POL._on_session_end(session_id="s-1", task_id="root", completed=True, failed=False, interrupted=False)
        assert not psutil.pid_exists(dpid) or psutil.Process(dpid).status() == psutil.STATUS_ZOMBIE
    finally:
        try:
            psutil.Process(dpid).kill()
        except Exception:
            pass
