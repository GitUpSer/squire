"""Fail-safe daemon ownership when a runtime dir is shared by live owned work (review finding 6b).

A runtime dir is per (process, origin, session[, BU_NAME]). Two owned works of this process can use
it at once (browser_capacity > 1, or a session end racing a late-finalizing call). The daemon in it
must never be signalled from under live work: terminate() skips it, a session end defers it, and the
last owned work to close performs the deferred stop.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from squire_overlay.guardrails import owned as OW
from squire_overlay.guardrails import policy as POL

from .fake_harness import FAKE_DAEMON


@pytest.fixture(autouse=True)
def _clean():
    yield
    OW.reset_for_tests()


def _dead(pid: int) -> bool:
    return not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


def _start_daemon(rt: str) -> int:
    p = subprocess.Popen([sys.executable, "-c", FAKE_DAEMON], start_new_session=True, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         env={"PATH": "/usr/bin:/bin", "BH_RUNTIME_DIR": rt, "HERMES_GUARDRAIL_OWNER": OW._owner_marker(rt)})
    for _ in range(100):
        if (Path(rt) / "bu.pid").exists() and (Path(rt) / "bu.sock").exists():
            break
        time.sleep(0.05)
    return p.pid


def test_terminate_never_stops_a_daemon_another_live_owned_work_is_using():
    rt = OW.runtime_dir_for("o:s")
    dpid = _start_daemon(rt)
    a = OW.OwnedWork(key="a", runtime_dir=rt)
    b = OW.OwnedWork(key="b", runtime_dir=rt)        # live sibling using the same daemon
    try:
        rep = a.terminate(grace_s=1.0)
        assert rep["daemon"]["reason"] == "in_use_by_other_owned_work" and b.id in rep["daemon"]["others"]
        assert not _dead(dpid) and a.killed_all is False and rep["marked_killed"] == []
        assert a.alive(include_daemon=False) == [], "only this call's own groups are verified"
        b.close()                                       # sibling gone → a full stop is allowed again
        rep2 = a.terminate(grace_s=2.0)
        assert rep2["daemon"]["stopped"] is True and _dead(dpid)
    finally:
        a.close(); b.close()
        if not _dead(dpid):
            psutil.Process(dpid).kill()


def test_session_end_defers_the_daemon_stop_until_the_last_owned_work_closes(monkeypatch):
    group = "agent_whatsapp_dm_111:s-7"
    rt = OW.runtime_dir_for(group, group=group)
    dpid = _start_daemon(rt)
    w = OW.OwnedWork(key="late", runtime_dir=rt)      # a call still executing when the session ends
    try:
        rep = POL._stop_session_daemon("agent_whatsapp_dm_111", "s-7")
        assert rep["reason"] == "deferred_in_use" and rep["stopped"] is False and w.id in rep["others"]
        assert not _dead(dpid) and OW.known_runtime_dirs(group) == [rt], "marker kept: still owned"
        w.close()                                       # last user closes → deferred stop runs now
        for _ in range(100):
            if _dead(dpid):
                break
            time.sleep(0.05)
        assert _dead(dpid)
        assert OW.known_runtime_dirs(group) == [] and OW._owner_marker(rt) == ""
    finally:
        if not _dead(dpid):
            psutil.Process(dpid).kill()


def test_named_session_gets_its_own_runtime_dir_inside_the_session_group():
    group = "o:s"
    plain = OW.runtime_dir_for(group, group=group)
    named = OW.runtime_dir_for(group + ":r7k2", group=group)
    assert plain != named, "one daemon per runtime dir is the harness contract; two BU_NAMEs must not share one"
    assert OW.known_runtime_dirs(group) == sorted([plain, named])
    assert OW.known_runtime_dir(group) == plain and OW.known_runtime_dir(group + ":r7k2") == named
