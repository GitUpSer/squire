"""Review #2 fixes 2/3/4: startup-race cleanup, unpredictable per-process ownership, spawn adapter
contract vs upstream, sanitized errors. All through the REAL ``tools.browser_use_cli`` seam.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

from squire_overlay.guardrails import deadline as D
from squire_overlay.guardrails import owned as OW
from squire_overlay.guardrails import policy as POL

from .fake_harness import FAKE_CLI_RACE, FAKE_DAEMON


def _dead(pid: int) -> bool:
    return not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


@pytest.fixture
def seam():
    OW.install_spawn_seam()
    yield
    OW.reset_for_tests()


@pytest.fixture(autouse=True)
def _clean_owned():
    yield
    OW.reset_for_tests()


# --- fix 2: pre-bu.pid startup race, plain-launch fallback (no scope) -----------------------

def test_deadline_during_daemon_startup_race_kills_detached_daemon_and_unrelated_survives(seam, hermes_env):
    """Fake CLI detaches a daemon that writes side effects for 3 s BEFORE publishing bu.pid; the
    deadline fires at 1 s. Nothing owned may survive; an unrelated daemon-lookalike must."""
    from tools import browser_use_cli as BU
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; d='browser_harness.daemon'; time.sleep(30)"],
                                 start_new_session=True, env={"PATH": "/usr/bin:/bin", "BH_RUNTIME_DIR": "/tmp/not-ours"})
    try:
        work = OW.OwnedWork(key="race-1")   # scope=None → plain launch (systemd unavailable path)
        env = {"FAKE_PID_DELAY_S": "3"}

        def call(_):
            try:
                return BU._run_cli_killing_process_group([sys.executable, "-c", FAKE_CLI_RACE], "", env, 100)
            except subprocess.TimeoutExpired:
                return "timeout"

        t0 = time.monotonic()
        out = D.run_with_deadline(call, {}, deadline_at=time.monotonic() + 1.0, kill_grace_s=1.0,
                                  cleanup_reserve_s=3.0, work=work, finalize=lambda: None)
        assert out.reason == "deadline_exceeded" and time.monotonic() - t0 < 10
        rt = work.runtime_dir
        assert rt and not (Path(rt) / "bu.pid").exists(), "race: bu.pid must NOT have been published yet"
        rep = out.owned_report
        assert rep["daemon"]["reason"] == "no_pid_file"
        assert rep["marked_killed"], "the detached pre-pid daemon must be found by its exact env marker and killed"
        assert rep["alive_after"] == [] and out.finalized is True and work.cleanup_unconfirmed is False
        for pid in rep["marked_killed"]:
            assert _dead(pid)
        marker = Path(rt) / "marker"
        s1 = marker.stat().st_size; time.sleep(0.4); s2 = marker.stat().st_size
        assert s1 == s2, "side effects must have stopped"
        assert not _dead(unrelated.pid), "unrelated daemon-lookalike must survive"
        assert OW.marked_processes(rt) == []
    finally:
        unrelated.kill(); unrelated.wait()


def test_finalizer_fails_closed_when_owned_process_cannot_be_confirmed_dead(hermes_env, monkeypatch):
    """If something owned is still alive after every kill step, the lease is NOT released."""
    released = []
    work = OW.OwnedWork(key="fc-1")
    rt = OW.runtime_dir_for("fc-1"); work.runtime_dir = rt
    # an owned-marked process that ignores SIGTERM/SIGKILL from our point of view: simulate by
    # making the kill a no-op (the process really is ours and really is alive)
    ours = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True,
                            env={"PATH": "/usr/bin:/bin", "BH_RUNTIME_DIR": rt, "HERMES_GUARDRAIL_OWNER": OW._owner_marker(rt)})
    try:
        monkeypatch.setattr(OW.os, "kill", lambda pid, sig: None)
        out = D.run_with_deadline(lambda _: time.sleep(5), {}, deadline_at=time.monotonic() + 0.2, kill_grace_s=0.3,
                                  cleanup_reserve_s=6.0, work=work, finalize=lambda: released.append(1))
        assert out.reason == "deadline_exceeded" and out.worker_finished is True
        assert work.cleanup_unconfirmed is True and released == [], "lease must NOT be released"
        assert work.deferred_finalize is not None and work in OW.live_work()
        assert any(a["pid"] == ours.pid for a in out.owned_report["alive_after"])
        # recover: kill works again → verified dead → retained lease released exactly once
        monkeypatch.undo()
        txt = POL.recover_text()
        assert "retained lease released" in txt and released == [1], txt
        assert _dead(ours.pid) and work not in OW.live_work()
    finally:
        with contextlib.suppress(Exception):
            ours.kill(); ours.wait()


# --- fix 3: unpredictable per-process ownership ---------------------------------------------

def test_runtime_dir_is_per_process_and_marker_in_memory_only(hermes_env):
    rt = OW.runtime_dir_for("origin:session")
    assert rt == OW.runtime_dir_for("origin:session"), "stable within the process (warm daemon)"
    marker = OW._owner_marker(rt)
    assert len(marker) >= 32
    on_disk = (Path(rt) / "owner.json").read_text()
    assert marker not in on_disk and "origin:session" not in on_disk, "marker never persisted"
    # another Hermes process computes a DIFFERENT dir for the same (origin, session)
    code = ("import sys; sys.path.insert(0, %r); from squire_overlay.guardrails import owned as OW; "
            "print(OW.runtime_dir_for('origin:session'))" % str(Path(OW.__file__).resolve().parents[2]))
    other = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
    assert other.returncode == 0, other.stderr
    assert other.stdout.strip() != rt


def test_pre_existing_daemon_from_previous_process_is_never_adopted_or_killed(hermes_env):
    """Simulate a gateway restart: a daemon from the OLD process lives in its runtime dir with its
    marker, bu.pid points at it. The NEW process (us) must neither see it as ours nor kill it."""
    old_rt = str(Path(OW._short_tmp()) / "deadbeefdeadbeef")
    Path(old_rt).mkdir(exist_ok=True)
    old = subprocess.Popen([sys.executable, "-c", FAKE_DAEMON], start_new_session=True,
                           env={"PATH": "/usr/bin:/bin", "BH_RUNTIME_DIR": old_rt, "HERMES_GUARDRAIL_OWNER": "old-process-token"},
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            if (Path(old_rt) / "bu.pid").exists():
                break
            time.sleep(0.05)
        (Path(old_rt) / "owner.json").write_text(json.dumps({"owner": "origin:session", "hermes_pid": 1}))
        assert OW.daemon_is_ours(old.pid, old_rt) is False
        assert OW.marked_processes(old_rt) == []
        assert OW.stop_owned_daemon(old_rt, grace_s=0.5)["reason"] == "not_owned"
        assert OW.kill_marked(old_rt) == []
        # a session-end for the same (origin, session) in the new process touches nothing: this
        # process never created a runtime dir for it, so there is nothing to own
        assert OW.known_runtime_dir("origin-old:session-old") is None
        assert POL._stop_session_daemon("origin-old", "session-old") is None
        work = OW.OwnedWork(key="x", runtime_dir=old_rt)   # even if handed the old dir explicitly
        assert work.alive() == []
        assert work.terminate(grace_s=0.5)["alive_after"] == []
        assert not _dead(old.pid), "previous process's daemon must survive untouched"
    finally:
        old.kill(); old.wait()
        for n in ("bu.pid", "bu.sock", "owner.json", "marker"):
            (Path(old_rt) / n).unlink(missing_ok=True)
        Path(old_rt).rmdir()


def test_session_end_kills_marked_descendants_and_forgets_marker(hermes_env, monkeypatch):
    rt = OW.runtime_dir_for("agent_whatsapp_dm_111:s-9")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True,
                             env={"PATH": "/usr/bin:/bin", "BH_RUNTIME_DIR": rt, "HERMES_GUARDRAIL_OWNER": OW._owner_marker(rt)})
    try:
        rep = POL._stop_session_daemon("agent_whatsapp_dm_111", "s-9")
        assert rep["reason"] == "no_pid_file" and child.pid in rep["marked_killed"] and rep["cleanup_confirmed"]
        assert _dead(child.pid)
        assert OW.known_runtime_dir("agent_whatsapp_dm_111:s-9") is None and OW._owner_marker(rt) == ""
    finally:
        child.kill(); child.wait()


# --- fix 4: adapter vs upstream behavioural contract -----------------------------------------

def _both(cmd, code, env, timeout):
    """Run upstream's untouched function and owned_launch side by side; return (upstream, owned)."""
    work = OW.OwnedWork(key="contract")
    try:
        up = {"r": None, "e": None}
        ow = {"r": None, "e": None}
        try:
            up["r"] = OW._installed_original(cmd, code, dict(env), timeout)
        except BaseException as e:
            up["e"] = e
        try:
            ow["r"] = OW.owned_launch(cmd, code, dict(env), timeout, work)
        except BaseException as e:
            ow["e"] = e
        return up, ow, work
    finally:
        work.terminate(grace_s=1.0); work.close()


def test_owned_launch_matches_upstream_on_success(seam, hermes_env):
    cmd = [sys.executable, "-c", "import sys; d=sys.stdin.read(); print('out:'+d); print('err', file=sys.stderr)"]
    up, ow, work = _both(cmd, "payload", {"PATH": "/usr/bin:/bin"}, 20)
    assert up["e"] is None and ow["e"] is None
    for r in (up["r"], ow["r"]):
        assert isinstance(r, subprocess.CompletedProcess) and r.args == cmd
    assert (ow["r"].returncode, ow["r"].stdout, ow["r"].stderr) == (up["r"].returncode, up["r"].stdout, up["r"].stderr)
    assert ow["r"].stdout == "out:payload\n"
    assert work.pgids == [work.procs[0].pid]


def test_owned_launch_matches_upstream_on_nonzero_exit(seam, hermes_env):
    cmd = [sys.executable, "-c", "import sys; print('partial'); sys.exit(7)"]
    up, ow, _ = _both(cmd, "", {"PATH": "/usr/bin:/bin"}, 20)
    assert up["e"] is None and ow["e"] is None
    assert (ow["r"].returncode, ow["r"].stdout, ow["r"].args) == (up["r"].returncode, up["r"].stdout, cmd) == (7, "partial\n", cmd)


def test_owned_launch_matches_upstream_on_timeout_and_never_leaks_scoped_argv(seam, hermes_env):
    cmd = [sys.executable, "-c", "import os, time; print(os.environ['LEAK'], flush=True); time.sleep(30)"]
    work = OW.OwnedWork(key="to")  # container: no systemd user scope
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as ei:
        OW.owned_launch(cmd, "", {"PATH": "/usr/bin:/bin", "LEAK": "SECRET-partial",
                                  **{k: os.environ[k] for k in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS") if k in os.environ}}, 0.5, work)
    assert time.monotonic() - t0 < 5
    exc = ei.value
    assert exc.cmd == cmd and exc.timeout == 0.5
    assert exc.output is None and exc.stderr is None, "partial output must not ride on the exception"
    text = str(exc)
    assert "systemd-run" not in text and "MemoryMax" not in text and "SECRET" not in text, text
    assert "systemd-run" not in repr(exc.cmd) and "SECRET" not in repr(exc.__dict__), exc.__dict__
    assert all(_dead(p) for p in work.pgids)
    # upstream raises the same type with the same cmd on timeout
    with pytest.raises(subprocess.TimeoutExpired) as eu:
        OW._installed_original(cmd, "", {"PATH": "/usr/bin:/bin", "LEAK": "x"}, 0.5)
    assert eu.value.cmd == cmd
    work.terminate(grace_s=1.0); work.close()


def test_owned_launch_sanitizes_launch_oserror(seam, hermes_env):
    work = OW.OwnedWork(key="ose")
    with pytest.raises(OSError) as ei:
        OW.owned_launch(["/nonexistent/secret-bin", "--token=abc"], "", {"PATH": "/usr/bin:/bin"}, 5, work)
    assert "secret-bin" not in str(ei.value) and "token" not in str(ei.value) and ei.value.errno is not None
    work.close()


def test_adapter_forwards_to_untouched_upstream_without_owned_context(seam, hermes_env):
    from tools import browser_use_cli as BU
    assert OW.current() is None
    cmd = [sys.executable, "-c", "import os; print(os.environ.get('BH_RUNTIME_DIR', 'unset'))"]
    r = BU._run_cli_killing_process_group(cmd, "", {"PATH": "/usr/bin:/bin"}, 20)
    assert r.stdout.strip() == "unset", "no owned context → upstream path, no runtime dir injected"
    assert OW.live_work() == []
