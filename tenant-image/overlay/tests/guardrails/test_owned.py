"""Call-owned process/daemon tracking (review blockers 1, 3, 4, 9).

Ownership comes from the REAL spawn seam (``tools.browser_use_cli._run_cli_killing_process_group``
wrapped by ``owned.install_spawn_seam``), never from cmdline heuristics. The harness daemon is
owned through its runtime dir (``BH_RUNTIME_DIR``) and verified via ``/proc/<pid>/environ``.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import psutil
import pytest

from squire_overlay.guardrails import owned as OW

from .fake_harness import FAKE_CLI, FAKE_DAEMON  # noqa: E402



def _spawn_cli_through_seam(env: dict, timeout: float = 100):
    """Run the REAL upstream spawn function (seam installed) with the fake CLI as ``cmd``."""
    from tools import browser_use_cli as BU
    return BU._run_cli_killing_process_group([sys.executable, "-c", FAKE_CLI], "", env, timeout)


@pytest.fixture
def seam():
    OW.install_spawn_seam()
    yield
    OW.reset_for_tests()


def test_seam_wraps_real_spawn_function_and_records_owned_pgid(seam, hermes_env):
    work = OW.OwnedWork(key="k1")
    rec = {}

    def run():
        with OW.attach(work):
            try:
                _spawn_cli_through_seam({}, timeout=0.5)
            except subprocess.TimeoutExpired:
                rec["timeout"] = True

    t = threading.Thread(target=run); t.start(); t.join(10)
    assert rec.get("timeout") is True
    assert work.pgids, "the real Popen pgid must be registered through the seam"
    assert all(not psutil.pid_exists(p) or psutil.Process(p).status() == psutil.STATUS_ZOMBIE for p in work.pgids)


def test_terminate_kills_only_owned_pgid_unrelated_chrom_like_child_survives(seam, hermes_env):
    work = OW.OwnedWork(key="k2")
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; chrom='chromium-browser'; time.sleep(30)"],
                                 start_new_session=True)
    try:
        done = threading.Event()

        def run():
            with OW.attach(work):
                try:
                    _spawn_cli_through_seam({}, timeout=100)
                except subprocess.TimeoutExpired:
                    pass
            done.set()

        t = threading.Thread(target=run, daemon=True); t.start()
        for _ in range(100):
            if work.pgids:
                break
            time.sleep(0.05)
        assert work.pgids
        rep = work.terminate(grace_s=2.0)
        assert done.wait(10), "communicate() must return once the owned group is dead"
        assert rep["pgids_killed"] == list(work.pgids)
        assert psutil.pid_exists(unrelated.pid) and psutil.Process(unrelated.pid).status() != psutil.STATUS_ZOMBIE
    finally:
        unrelated.kill(); unrelated.wait()


def test_owned_daemon_is_shut_down_and_marker_stops(seam, hermes_env, tmp_path):
    work = OW.OwnedWork(key="k3")
    env = {}
    done = threading.Event()

    def run():
        with OW.attach(work):
            try:
                _spawn_cli_through_seam(env, timeout=100)
            except subprocess.TimeoutExpired:
                pass
        done.set()

    t = threading.Thread(target=run, daemon=True); t.start()
    rt = None
    for _ in range(200):
        rt = work.runtime_dir
        if rt and (Path(rt) / "bu.pid").exists() and (Path(rt) / "bu.sock").exists():
            break
        time.sleep(0.05)
    assert rt and (Path(rt) / "bu.pid").exists(), "seam must inject an OWNED BH_RUNTIME_DIR the daemon publishes into"
    assert env.get("BH_RUNTIME_DIR") == rt
    dpid = int((Path(rt) / "bu.pid").read_text())
    assert OW.daemon_is_ours(dpid, rt) is True
    rep = work.terminate(grace_s=2.0)
    assert done.wait(10)
    assert rep["daemon"]["stopped"] is True and rep["daemon"]["pid"] == dpid
    assert not psutil.pid_exists(dpid) or psutil.Process(dpid).status() == psutil.STATUS_ZOMBIE
    marker = Path(rt) / "marker"
    size1 = marker.stat().st_size; time.sleep(0.3); size2 = marker.stat().st_size
    assert size1 == size2, "the detached daemon must have STOPPED producing side effects"
    assert work.alive() == [], "nothing owned may be left alive after terminate()"


def test_daemon_ownership_is_verified_not_guessed(hermes_env, tmp_path):
    """A pid file pointing at a process NOT started with our runtime dir is never signalled."""
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        d = tmp_path / "rt"; d.mkdir()
        (d / "bu.pid").write_text(str(other.pid))
        assert OW.daemon_is_ours(other.pid, str(d)) is False
        rep = OW.stop_owned_daemon(str(d), grace_s=0.5)
        assert rep["stopped"] is False and rep["reason"] == "not_owned"
        assert psutil.pid_exists(other.pid) and psutil.Process(other.pid).status() != psutil.STATUS_ZOMBIE
    finally:
        other.kill(); other.wait()


def test_container_launch_preserves_argv_without_systemd(seam, hermes_env):
    work = OW.OwnedWork(key="container")
    with OW.attach(work):
        from tools import browser_use_cli as BU
        cp = BU._run_cli_killing_process_group([sys.executable, "-c", "print('container-launch')"], "", {}, 5)
    assert cp.returncode == 0 and cp.stdout.strip() == "container-launch"
    assert work.scope_applied is False
    assert work.pgids
