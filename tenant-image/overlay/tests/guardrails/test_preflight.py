"""Resource preflight / admission (acceptance item 4). Injected readings; no live-system change."""

from __future__ import annotations

from squire_overlay.guardrails import preflight as P


def _r(load1=0.5, ncpu=2, avail_mb=2000, swap_used_pct=10, chromium=0):
    return P.Readings(load1=load1, ncpu=ncpu, avail_mb=avail_mb, swap_used_pct=swap_used_pct, chromium_procs=chromium)


def test_admits_when_host_is_healthy():
    v = P.admit(_r())
    assert v.allowed and v.reason == "ok"


def test_refuses_new_browser_launch_under_load():
    v = P.admit(_r(load1=22.0))
    assert not v.allowed and v.reason == "host_pressure_load" and v.metrics["load1"] == 22.0


def test_refuses_when_memory_low_or_swap_heavy():
    assert P.admit(_r(avail_mb=200)).reason == "host_pressure_memory"
    assert P.admit(_r(swap_used_pct=85)).reason == "host_pressure_swap"


def test_refuses_more_chromium_than_cap():
    v = P.admit(_r(chromium=2), max_chromium=1)
    assert not v.allowed and v.reason == "host_pressure_chromium_count"


def test_pressure_refusal_names_the_blocker_for_the_user():
    msg = P.admit(_r(load1=22.0)).message
    assert "load" in msg and "22.0" in msg and "retry" in msg.lower()


def test_live_readings_have_expected_shape():
    r = P.read_host()
    assert r.ncpu >= 1 and r.avail_mb >= 0 and 0 <= r.swap_used_pct <= 100 and r.chromium_procs >= 0


def test_chromium_count_ignores_helpers_and_crashpad(monkeypatch):
    class FakeProc:
        def __init__(self, name, cmd):
            self.info = {"name": name, "cmdline": cmd.split()}

    procs = [
        FakeProc("chrome", "/x/chrome --remote-debugging-port=9333 --headless=new"),
        FakeProc("chrome_crashpad", "/x/chrome_crashpad_handler --monitor-self"),
        FakeProc("chrome", "/x/chrome --type=zygote"),
        FakeProc("chrome", "/x/chrome --type=renderer"),
        FakeProc("python3", "python3 -c print(1)"),
    ]
    import psutil
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: procs)
    assert P.read_host().chromium_procs == 1


def test_orphan_harness_daemons_are_counted_and_surfaced(monkeypatch):
    class FakeProc:
        def __init__(self, cmd, etime):
            self.info = {"name": "python", "cmdline": cmd.split(), "create_time": 1000.0 - etime}

    procs = [FakeProc("/x/python -m browser_harness.daemon", 90000), FakeProc("/x/python -m browser_harness.daemon", 10)]
    import psutil
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: procs)
    monkeypatch.setattr(P.time, "time", lambda: 1000.0)
    r = P.read_host()
    assert r.harness_daemons == 2 and r.stale_harness_daemons == 1
    assert "harness_daemons" in r.as_dict()


def test_systemd_dropin_artifact_is_well_formed_and_not_applied(tmp_path):
    text = P.render_systemd_dropin()
    assert "CPUWeight=" in text and "MemoryHigh=" in text and "[Scope]" in text or "[Service]" in text
    # artifact only: nothing under /etc or ~/.config is written by rendering
    assert not any(p.exists() for p in P.dropin_install_paths() if "latency-guardrails" in str(p))
