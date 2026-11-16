"""Deadline v2: ONE absolute monotonic deadline, ownership-exact cancellation, late finalization.

Review blockers 1 + 2. Real processes, real threads; nothing mocked except the downstream tool.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time

import psutil

from squire_overlay.guardrails import deadline as D
from squire_overlay.guardrails import owned as OW


def _spawn_owned_then_block(record: dict, extra_block_s: float = 0.0):
    """Downstream stand-in that spawns through the ownership seam and blocks like communicate()."""

    def call(args):
        work = OW.current()
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        work.spawned(proc)
        record["pid"] = proc.pid
        proc.communicate()
        if extra_block_s:
            time.sleep(extra_block_s)   # a slow post-kill drain upstream
        record["returned_at"] = time.monotonic()
        return '{"success": false}'

    return call


def test_absolute_deadline_fires_at_deadline_at_not_after_a_fresh_budget(hermes_env):
    record: dict = {}
    work = OW.OwnedWork(key="d1")
    t0 = time.monotonic()
    out = D.run_with_deadline(_spawn_owned_then_block(record), {}, deadline_at=t0 + 1.0, kill_grace_s=2.0,
                              work=work, finalize=lambda: None)
    elapsed = time.monotonic() - t0
    assert out.reason == "deadline_exceeded" and out.killed_pids == [record["pid"]]
    assert 1.0 <= elapsed < 4.0, elapsed
    assert out.worker_finished is True
    assert not psutil.pid_exists(record["pid"]) or psutil.Process(record["pid"]).status() == psutil.STATUS_ZOMBIE


def test_already_expired_deadline_is_not_floored_and_does_not_run_downstream(hermes_env):
    ran = []
    out = D.run_with_deadline(lambda a: ran.append(1), {}, deadline_at=time.monotonic() - 0.1, kill_grace_s=1.0,
                              work=OW.OwnedWork(key="d2"), finalize=lambda: None)
    assert out.reason == "deadline_exceeded" and ran == [] and out.worker_finished is True


def test_late_finalization_releases_only_after_worker_returns_and_owned_work_dead(hermes_env):
    record: dict = {}
    released = []
    work = OW.OwnedWork(key="d3")
    t0 = time.monotonic()
    out = D.run_with_deadline(_spawn_owned_then_block(record, extra_block_s=2.0), {}, deadline_at=t0 + 0.5,
                              kill_grace_s=0.5, cleanup_reserve_s=0.5, work=work,
                              finalize=lambda: released.append(time.monotonic()))
    # returned to the caller BEFORE the worker finished (bounded), finalize NOT yet run
    assert out.worker_finished is False and released == []
    assert time.monotonic() - t0 < 2.5
    # ... and the finalizer runs by itself once the worker returns (no restart, no operator)
    for _ in range(100):
        if released:
            break
        time.sleep(0.1)
    assert released and "returned_at" in record and released[0] >= record["returned_at"]
    assert work.alive() == []


def test_unrelated_chrom_like_child_spawned_mid_call_survives(hermes_env):
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; x='chromium'; time.sleep(30)"],
                                 start_new_session=True)
    try:
        record: dict = {}
        D.run_with_deadline(_spawn_owned_then_block(record), {}, deadline_at=time.monotonic() + 0.5,
                            kill_grace_s=1.0, work=OW.OwnedWork(key="d4"), finalize=lambda: None)
        assert psutil.pid_exists(unrelated.pid) and psutil.Process(unrelated.pid).status() != psutil.STATUS_ZOMBIE
    finally:
        unrelated.kill(); unrelated.wait()
