"""Host-wide interactive-browser lease (acceptance item 1).

Real file locks in a temp HERMES_HOME; a second PROCESS competes for the same lease so
the test proves cross-process exclusion and crash recovery, not just thread exclusion.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from squire_overlay.guardrails import lease as L


def _state_dir(hermes_env) -> Path:
    return hermes_env / "guardrails"


def test_lease_dir_derives_from_hermes_home(hermes_env):
    assert L.lease_dir() == _state_dir(hermes_env) / "leases"


def test_single_holder_then_second_acquire_times_out_with_reason(hermes_env):
    a = L.acquire("browser", owner="task-a", wait_s=0.0)
    assert a.acquired and a.reason == "acquired"
    b = L.acquire("browser", owner="task-b", wait_s=0.2)
    assert not b.acquired
    assert b.reason == "lease_wait_timeout"
    assert b.holder == "task-a"          # structured: tells the caller WHO holds it
    assert b.waited_s >= 0.2
    a.release()
    c = L.acquire("browser", owner="task-c", wait_s=0.0)
    assert c.acquired
    c.release()


def test_release_is_idempotent_and_ownership_bound(hermes_env):
    a = L.acquire("browser", owner="x", wait_s=0.0)
    a.release()
    a.release()  # no raise
    b = L.acquire("browser", owner="y", wait_s=0.0)
    assert b.acquired
    b.release()


def test_session_names_do_not_bypass_the_lease(hermes_env):
    """Same lease name regardless of browser session label: 'samsung' and 'pixel' queue."""
    a = L.acquire(L.lease_name_for_browser(session="samsung"), owner="a", wait_s=0)
    b = L.acquire(L.lease_name_for_browser(session="pixel"), owner="b", wait_s=0)
    assert a.acquired and not b.acquired and b.reason == "lease_wait_timeout"
    a.release()


def test_capacity_above_one_allows_that_many_holders(hermes_env):
    a = L.acquire("web", owner="a", wait_s=0, capacity=2)
    b = L.acquire("web", owner="b", wait_s=0, capacity=2)
    c = L.acquire("web", owner="c", wait_s=0, capacity=2)
    assert a.acquired and b.acquired and not c.acquired
    a.release(); b.release()


def test_queue_is_bounded(hermes_env):
    a = L.acquire("browser", owner="holder", wait_s=0)
    assert a.acquired
    # max_queue=1: first waiter is queued (and times out), second is rejected immediately.
    import threading
    results = {}

    def waiter(name):
        results[name] = L.acquire("browser", owner=name, wait_s=1.0, max_queue=1)

    t = threading.Thread(target=waiter, args=("w1",)); t.start()
    time.sleep(0.2)
    r2 = L.acquire("browser", owner="w2", wait_s=1.0, max_queue=1)
    assert not r2.acquired and r2.reason == "lease_queue_full"
    assert r2.waited_s < 0.5
    t.join()
    assert results["w1"].reason == "lease_wait_timeout"
    a.release()


_CHILD = textwrap.dedent(
    """
    import os, sys, time
    sys.path.insert(0, sys.argv[1])
    from squire_overlay.guardrails import lease as L
    r = L.acquire("browser", owner="child-proc", wait_s=0.0)
    print("ACQUIRED" if r.acquired else "BUSY:" + r.reason + ":" + str(r.holder), flush=True)
    if r.acquired and sys.argv[2] == "hold":
        # hold until killed — simulates a crash while holding the lease
        time.sleep(60)
    """
)


def _spawn_child(mode: str, plugin_dir: str, env: dict) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", _CHILD, plugin_dir, mode],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )


def test_second_process_sees_holder_and_crash_releases_lease(hermes_env):
    plugin_dir = str(Path(L.__file__).resolve().parents[2])
    env = dict(os.environ, HERMES_HOME=str(hermes_env))
    env.pop("PYTHONPATH", None)
    # Child holds the lease; parent must fail with the child's owner id.
    child = _spawn_child("hold", plugin_dir, env)
    line = child.stdout.readline().strip()
    assert line == "ACQUIRED", (line, child.stderr.read())
    r = L.acquire("browser", owner="parent", wait_s=0.3)
    assert not r.acquired and r.reason == "lease_wait_timeout" and r.holder == "child-proc"
    # Crash the holder (SIGKILL: no release() runs). The kernel drops the flock.
    child.kill(); child.wait(timeout=5)
    r2 = L.acquire("browser", owner="parent", wait_s=2.0)
    assert r2.acquired, r2
    assert r2.recovered_from_crash is True  # stale holder record detected and reported
    r2.release()
    # And the reverse: parent holds, a fresh process is refused with our owner id.
    a = L.acquire("browser", owner="parent-hold", wait_s=0)
    child2 = _spawn_child("once", plugin_dir, env)
    out, err = child2.communicate(timeout=20)
    assert out.strip() == "BUSY:lease_wait_timeout:parent-hold", (out, err)
    a.release()


def test_holder_info_is_readable_for_status(hermes_env):
    a = L.acquire("browser", owner="task-z", wait_s=0, meta={"tool": "browser_exec"})
    info = L.holders("browser")
    assert info and info[0]["owner"] == "task-z" and info[0]["pid"] == os.getpid()
    assert info[0]["meta"]["tool"] == "browser_exec"
    a.release()
    assert L.holders("browser") == []


_WAITER = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, sys.argv[1])
    from squire_overlay.guardrails import lease as L
    # queues behind the parent's holder; max_queue=1 so a second waiter would be refused
    L.acquire("browser", owner="waiter-proc", wait_s=30.0, max_queue=1)
    """
)


def test_killed_waiter_does_not_inflate_the_queue(hermes_env):
    """The waiter count must be kernel-released like the slot flock: a SIGKILLed waiter's ticket is
    reaped on the next count, so the queue never fills up with ghosts (no manual reset needed)."""
    plugin_dir = str(Path(L.__file__).resolve().parents[2])
    env = {**os.environ, "HERMES_HOME": os.environ["HERMES_HOME"], "PATH": "/usr/bin:/bin"}
    holder = L.acquire("browser", owner="holder", wait_s=0)
    assert holder.acquired
    child = subprocess.Popen([sys.executable, "-c", _WAITER, plugin_dir], env=env)
    try:
        for _ in range(100):
            if L.queue_depth("browser") == 1:
                break
            time.sleep(0.05)
        assert L.queue_depth("browser") == 1
        r = L.acquire("browser", owner="w2", wait_s=0.3, max_queue=1)
        assert r.reason == "lease_queue_full"
        child.kill(); child.wait()
        assert L.queue_depth("browser") == 0, "dead waiter's ticket must be reaped"
        assert L.prune_queue("browser") == {"live": 0, "pruned": 0}
        r = L.acquire("browser", owner="w3", wait_s=0.3, max_queue=1)
        assert r.reason == "lease_wait_timeout", "a new waiter is admitted into the queue again"
    finally:
        if child.poll() is None:
            child.kill(); child.wait()
        holder.release()
