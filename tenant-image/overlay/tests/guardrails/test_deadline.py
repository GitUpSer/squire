"""End-to-end deadline + cancellable worker lifecycle (acceptance item 2), v2 contract.

Uses a REAL hung child process registered through the ownership seam, so the test proves: the
absolute deadline fires, ONLY the owned process group is killed within a bounded grace, no survivor
remains, and the finalizer (lease release) runs only after the worker has actually returned.
"""

from __future__ import annotations

import subprocess
import sys
import time

import psutil

from squire_overlay.guardrails import deadline as D
from squire_overlay.guardrails import owned as OW


def _hung_child_call(record: dict):
    """next_call stand-in behaving like upstream browser_exec under the seam: spawns a child in a new
    session, registers it as owned, blocks on communicate() until the child exits."""

    def call(args):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
        OW.current().spawned(proc)
        record["pid"] = proc.pid
        proc.communicate()  # returns only when the child dies
        record["returned_at"] = time.monotonic()
        return '{"success": false, "error": "upstream saw the child die"}'

    return call


def test_hung_worker_killed_within_bounded_grace_no_survivor(hermes_env):
    record: dict = {}
    released = []
    t0 = time.monotonic()
    out = D.run_with_deadline(
        _hung_child_call(record), {"code": "x"}, deadline_at=t0 + 1.0, kill_grace_s=5.0,
        work=OW.OwnedWork(key="t1"), finalize=lambda: released.append(time.monotonic()),
    )
    elapsed = time.monotonic() - t0
    assert out.reason == "deadline_exceeded"
    assert out.killed_pids == [record["pid"]]
    assert elapsed < 6.0, elapsed                        # 1s budget + bounded kill/drain, never 120s
    assert not psutil.pid_exists(record["pid"]) or psutil.Process(record["pid"]).status() == psutil.STATUS_ZOMBIE
    # the worker thread actually returned, and the release callback ran AFTER it did
    assert "returned_at" in record and released and released[0] >= record["returned_at"]
    assert out.worker_finished is True and out.finalized is True
    assert out.phases["execution_s"] >= 1.0 and out.phases["cleanup_s"] >= 0.0


def test_legitimate_fast_work_passes_through_untouched(hermes_env):
    out = D.run_with_deadline(lambda a: '{"success": true, "output": "hi"}', {"code": "print(1)"},
                              deadline_at=time.monotonic() + 5.0, kill_grace_s=1.0)
    assert out.reason == "ok" and out.result == '{"success": true, "output": "hi"}'
    assert out.killed_pids == [] and out.worker_finished


def test_overrun_is_reported_when_upstream_ignores_its_timeout(hermes_env):
    """Upstream returned, but later than the deadline: must be flagged, not silent."""
    def slow(args):
        time.sleep(1.2)
        return '{"success": true}'
    out = D.run_with_deadline(slow, {}, deadline_at=time.monotonic() + 0.5, kill_grace_s=3.0, cleanup_reserve_s=3.0)
    assert out.reason == "deadline_exceeded"
    assert out.overrun_s >= 0.5


def test_syntax_invalid_code_is_rejected_before_any_startup():
    err = D.precheck_code("def broken(:\n  pass")
    assert err is not None and err["reason"] == "code_syntax_error" and "line" in err
    assert D.precheck_code("print('ok')\nx = [i for i in range(3)]") is None
