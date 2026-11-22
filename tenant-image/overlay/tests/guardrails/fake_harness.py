"""Shared fake browser-harness daemon/CLI scripts for ownership tests."""

from __future__ import annotations

import textwrap

# A stand-in for ``browser_harness.daemon`` (PyPI package browser-harness 0.1.13, pulled in by the
# browser-use CLI): same contract — reads BH_RUNTIME_DIR, publishes <dir>/bu.pid, serves newline-JSON
# {"meta":"ping"} / {"meta":"shutdown"} → {"ok": true} on <dir>/bu.sock, detaches with
# start_new_session — and writes a heartbeat marker so a test can prove it STOPPED. The real contract
# is pinned by overlay/tests/contract/test_browser_harness_contract.py, which also checks this fake.
FAKE_DAEMON = textwrap.dedent("""
    import json, os, socket, sys, threading, time
    d = os.environ["BH_RUNTIME_DIR"]
    time.sleep(float(os.environ.get("FAKE_PID_DELAY_S", "0")))   # startup race window (bu.pid not yet published)
    open(os.path.join(d, "bu.pid"), "w").write(str(os.getpid()))
    stop = threading.Event()
    def serve():
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(os.path.join(d, "bu.sock")); s.listen(4)
        while not stop.is_set():
            c, _ = s.accept()
            req = json.loads(c.recv(65536).decode() or "{}")
            if req.get("meta") == "ping":
                c.sendall((json.dumps({"pong": True, "pid": os.getpid()}) + "\\n").encode())
            elif req.get("meta") == "shutdown":
                c.sendall(b'{"ok": true}\\n'); stop.set()
            c.close()
    threading.Thread(target=serve, daemon=True).start()
    marker = os.path.join(d, "marker")
    while not stop.is_set():
        open(marker, "a").write("x"); time.sleep(0.05)
""")

# A stand-in for the browser-use CLI: spawns the detached daemon (like ensure_daemon) then
# blocks forever (like a wedged script).
FAKE_CLI = textwrap.dedent("""
    import os, subprocess, sys, time
    subprocess.Popen([sys.executable, "-c", %r], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(120)
""") % FAKE_DAEMON

# Daemon side effects that start BEFORE bu.pid exists (marker written during the delay) — proves a
# kill during the startup race stops the side effects, not just the eventual pid-file holder.
FAKE_DAEMON_EARLY_MARKER = textwrap.dedent("""
    import json, os, socket, sys, threading, time
    d = os.environ["BH_RUNTIME_DIR"]
    marker = os.path.join(d, "marker")
    delay = float(os.environ.get("FAKE_PID_DELAY_S", "3"))
    t_end = time.monotonic() + delay
    while time.monotonic() < t_end:
        open(marker, "a").write("x"); time.sleep(0.05)
    open(os.path.join(d, "bu.pid"), "w").write(str(os.getpid()))
    while True:
        open(marker, "a").write("x"); time.sleep(0.05)
""")

FAKE_CLI_RACE = textwrap.dedent("""
    import os, subprocess, sys, time
    subprocess.Popen([sys.executable, "-c", %r], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(120)
""") % FAKE_DAEMON_EARLY_MARKER
