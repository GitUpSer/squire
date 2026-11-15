"""Call-owned process / daemon / resource-scope tracking (review blockers 1, 3, 4, 9).

Ownership is EXPLICIT, never heuristic:

* ``install_spawn_seam`` replaces ``tools.browser_use_cli._run_cli_killing_process_group`` (the
  one function upstream uses to spawn the browser-use CLI) with an ADAPTER: a call with no
  ``OwnedWork`` attached to its context is forwarded to the untouched upstream function; a call
  with one attached goes through ``owned_launch`` — the explicit owned launch primitive — which
  reuses upstream's helpers (``_group_popen_kwargs``, ``_kill_cli_process_group``,
  ``_POST_KILL_DRAIN_S``) and must stay behaviourally identical to upstream on success, failure
  and timeout. Nothing global (``subprocess.Popen``, ``BU.subprocess``) is ever patched, so the
  adapter is safe under threads/thread pools. ``overlay/tests/contract`` pins the upstream body by
  hash AND exercises both paths side by side (``test_owned.py``: success / non-zero exit / timeout).
* The browser-harness daemon the CLI auto-spawns is owned through its runtime dir: the seam injects
  a ``BH_RUNTIME_DIR`` that is unique to THIS Hermes process + owner key (the harness publishes
  ``bu.pid`` / ``bu.sock`` there) plus an ``HERMES_GUARDRAIL_OWNER`` marker that is a random token
  generated in this process and held ONLY in memory. A process is signalled ONLY after
  ``/proc/<pid>/environ`` proves it was started with that exact runtime dir + in-memory marker. A
  daemon from a previous Hermes process (gateway restart), from another Hermes process or from the
  user can therefore never be adopted or killed: no in-memory marker exists for it. Such daemons
  are orphans for this process — reported by preflight, never signalled (documented limit).

  THE DAEMON CONTRACT, VERIFIED (review finding 6). It is not in hermes-agent and not in the
  browser-use repo proper: it lives in the separate PyPI package ``browser-harness`` that
  browser-use pins (browser-use 0.13.10 pyproject: ``browser-harness==0.1.13``) and that
  ``uv tool install browser-use`` pulls into the CLI's own venv. Hermes upstream only touches it in
  ``tools/browser_use_cli.py:51-52`` (``from browser_harness import _ipc; _ipc.pid_path(BU_NAME)``)
  and never sets ``BH_RUNTIME_DIR``/``BH_TMP_DIR`` itself. In browser-harness 0.1.13:
    - ``_ipc.py:19,23``  ``BH_RUNTIME_DIR`` (env, read at import) is the daemon's runtime dir;
    - ``_ipc.py:41-43``  with it set (and ``BH_RUNTIME_DIR_SHARED`` unset) file stems are the bare
      ``bu`` — ONE daemon per runtime dir, whatever ``BU_NAME`` says; ``BU_NAME`` keys the files
      (``bu-<name>.pid``) only in the shared/default dir, which we never use;
    - ``_ipc.py:52,54``  ``pid_path`` → ``<dir>/bu.pid``, ``_sock_path`` → ``<dir>/bu.sock``;
    - ``_ipc.py:96-106`` requests are one JSON line + ``\n``, reply one JSON line; a token is
      required only on Windows (``_server_token`` stays None on POSIX, ``_ipc.py:28-32``);
    - ``daemon.py:677``  ``{"meta":"ping"}`` → ``{"pong": true, "pid": <daemon pid>}``;
    - ``daemon.py:740-762`` ``{"meta":"shutdown"}`` → ``{"ok": true}`` after a clean stop;
    - ``admin.py:562,579-588`` the daemon is spawned with ``env={**os.environ, …}`` (so it inherits
      our two env markers) and ``_ipc.spawn_kwargs()`` = ``start_new_session=True`` (``_ipc.py:80``)
      — i.e. detached from the CLI's process group, which is why env-marking is needed at all.
  ``overlay/tests/contract/test_browser_harness_contract.py`` pins all of this against the REAL
  package when it can be located (the CLI's uv-tool venv, an importable copy, or
  ``HERMES_BROWSER_HARNESS_SRC``) and against upstream's use of it; the fake harness used by the
  unit tests mirrors exactly that contract.
* Fail-safe sharing: a runtime dir is per (process, origin, session[, BU_NAME]) and can be in use by
  more than one ``OwnedWork`` of this process (``browser_capacity`` > 1, or a session-end racing a
  late-finalizing call). The daemon in a dir is never signalled while ANOTHER live owned work uses
  it: ``terminate`` skips the daemon/marked kill (reported as ``in_use_by_other_owned_work``) and a
  session end defers the stop (``mark_session_ended``) until the last owned work in that dir closes.
* Detached descendants started by the owned CLI BEFORE the daemon published ``bu.pid`` (startup
  race) carry the same env; ``kill_marked`` finds them by that exact env pair (no cmdline / name
  heuristics) and kills them too, before any lease is released.
* Resource scope (optional, default on when ``systemd-run --user`` works): the CLI argv is wrapped in a
  transient scope under ``hermes-browser.slice`` with MemoryMax / CPUWeight. The scope contains ONLY
  processes descended from this call, so stopping it is ownership-exact.

``OwnedWork.terminate`` = kill owned process groups → shut the owned daemon down (IPC shutdown,
SIGTERM, SIGKILL; verified) → kill every remaining env-marked process → stop the call's scope →
report. ``alive()`` must be empty afterwards; the finalizer fails CLOSED (lease not released) when
it is not.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import logging
import os
import secrets
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

OWNER_ENV = "HERMES_GUARDRAIL_OWNER"
RUNTIME_ENV = "BH_RUNTIME_DIR"
_CURRENT: contextvars.ContextVar = contextvars.ContextVar("guardrail_owned_work", default=None)
_installed_original = None
_lock = threading.Lock()
_registry: dict[str, "OwnedWork"] = {}        # key -> live work (this process)
_counter = 0
# Unpredictable per-process identity: runtime dirs and owner markers derive from it, so nothing
# started by another process (previous gateway, sibling Hermes, user) can ever match ours.
_PROCESS_TOKEN = secrets.token_hex(16)
_markers: dict[str, str] = {}                 # runtime_dir -> in-memory owner marker (this process only)
_groups: dict[str, set[str]] = {}             # session group key -> runtime dirs created for it (this process)
_ended_dirs: set[str] = set()                 # dirs whose session ended while owned work still used them


# ---------------------------------------------------------------------------------------------
# runtime dirs (owned daemons)
# ---------------------------------------------------------------------------------------------

def _short_tmp() -> Path:
    try:
        from hermes_constants import socket_safe_tmpdir
        base = Path(socket_safe_tmpdir())
    except Exception:
        base = Path("/tmp")  # noqa: S108 - AF_UNIX path length
    d = base / "hgb"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def runtime_dir_for(owner_key: str, *, group: Optional[str] = None) -> str:
    """Short BH_RUNTIME_DIR (AF_UNIX sun_path budget), unique to THIS process + owner key.

    Stable within the process (warm daemon across the calls of one session); different in every
    other process, so a restart never recomputes a dir that still holds another process's daemon.
    The owner marker is a fresh random token kept in memory; ``owner.json`` carries diagnostics only
    (hermes pid) and is never trusted for ownership. ``group`` (default: the owner key itself) is
    the session key the dir is stopped under at session end — a named ``session`` (BU_NAME) gets
    its own dir/daemon (one daemon per dir is the harness contract) inside the same session group."""
    h = hashlib.sha256(f"{_PROCESS_TOKEN}:{owner_key}".encode("utf-8")).hexdigest()[:16]
    d = _short_tmp() / h
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _lock:
        _markers.setdefault(str(d), secrets.token_hex(16))
        _groups.setdefault(group or owner_key, set()).add(str(d))
    with contextlib.suppress(OSError):
        (d / "owner.json").write_text(json.dumps({"hermes_pid": os.getpid(), "at": time.time()}))
    return str(d)


def known_runtime_dir(owner_key: str) -> Optional[str]:
    """The runtime dir THIS process created for ``owner_key``, or None (never creates / registers)."""
    h = hashlib.sha256(f"{_PROCESS_TOKEN}:{owner_key}".encode("utf-8")).hexdigest()[:16]
    d = str(_short_tmp() / h)
    with _lock:
        return d if d in _markers else None


def known_runtime_dirs(group: str) -> list[str]:
    """Every runtime dir THIS process created under session group ``group`` (still marked)."""
    with _lock:
        return sorted(d for d in _groups.get(group, set()) if d in _markers)


def shared_with_live_work(runtime_dir: str, *, exclude: Optional["OwnedWork"] = None) -> list["OwnedWork"]:
    """Live, not-yet-terminated owned work in this process that uses ``runtime_dir`` (registry check).
    Anything in this list may be talking to the daemon in that dir right now."""
    with _lock:
        works = list(_registry.values())
    return [w for w in works if w is not exclude and w.runtime_dir == str(runtime_dir) and not w.terminated]


def mark_session_ended(runtime_dir: str) -> None:
    """The session owning ``runtime_dir`` ended while owned work still used it: remember to stop the
    daemon when the LAST such work closes (see ``OwnedWork.close``)."""
    with _lock:
        _ended_dirs.add(str(runtime_dir))


def _maybe_stop_ended_dir(runtime_dir: Optional[str]) -> Optional[dict]:
    """Deferred session-end stop: runs from ``OwnedWork.close`` when no other live work uses the dir."""
    if not runtime_dir:
        return None
    with _lock:
        pending = str(runtime_dir) in _ended_dirs
    if not pending or shared_with_live_work(runtime_dir):
        return None
    rep = stop_owned_daemon(runtime_dir, grace_s=3.0)
    rep["marked_killed"] = kill_marked(runtime_dir, grace_s=3.0)
    rep["deferred"] = True
    if not marked_processes(runtime_dir):
        forget_runtime_dir(runtime_dir)
    with _lock:
        _ended_dirs.discard(str(runtime_dir))
    return rep


def _proc_environ(pid: int) -> dict[str, str]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    out: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if b"=" in item:
            k, _, v = item.partition(b"=")
            out[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
    return out


def _pid_status(pid: int) -> Optional[str]:
    try:
        import psutil
        p = psutil.Process(pid)
        return p.status()
    except Exception:
        return None


def _alive(pid: int) -> bool:
    st = _pid_status(pid)
    return st is not None and st != "zombie"


def _env_is_ours(env: dict, runtime_dir: str) -> bool:
    marker = _owner_marker(runtime_dir)
    return bool(marker) and env.get(RUNTIME_ENV) == runtime_dir and env.get(OWNER_ENV) == marker


def daemon_is_ours(pid: int, runtime_dir: str) -> bool:
    """True only when the live process was started with OUR runtime dir and this process's
    in-memory owner marker. Fails closed: unreadable environ or unknown runtime dir → False."""
    if pid <= 0 or not _alive(pid):
        return False
    env = _proc_environ(pid)
    return bool(env) and _env_is_ours(env, runtime_dir)


def _owner_marker(runtime_dir: str) -> str:
    """The in-memory marker for a runtime dir created by THIS process; '' for anything else."""
    with _lock:
        return _markers.get(str(runtime_dir), "")


def marked_processes(runtime_dir: str) -> list[int]:
    """Live pids whose environ carries OUR runtime dir + in-memory marker (ownership-exact scan;
    no cmdline/name heuristics). Empty when the dir is not ours."""
    if not _owner_marker(runtime_dir):
        return []
    out: list[int] = []
    try:
        import psutil
        for p in psutil.process_iter(["pid"]):
            pid = p.pid
            if pid == os.getpid() or not _alive(pid):
                continue
            if _env_is_ours(_proc_environ(pid), runtime_dir):
                out.append(pid)
    except Exception:
        pass
    return out


def kill_marked(runtime_dir: str, grace_s: float = 3.0) -> list[int]:
    """SIGTERM→SIGKILL every env-marked owned process (covers the pre-``bu.pid`` startup race and
    detached children such as the daemon's Chromium). Returns the pids signalled."""
    pids = marked_processes(runtime_dir)
    for pid in pids:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + max(0.2, grace_s)
    while time.monotonic() < deadline and any(_alive(p) for p in pids):
        time.sleep(0.05)
    for pid in pids:
        if _alive(pid):
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and any(_alive(p) for p in pids):
        time.sleep(0.05)
    return pids


def forget_runtime_dir(runtime_dir: str) -> None:
    """Drop the in-memory marker (session over) and remove the dir if it is empty of sockets."""
    with _lock:
        _markers.pop(str(runtime_dir), None)
        for dirs in _groups.values():
            dirs.discard(str(runtime_dir))
        _ended_dirs.discard(str(runtime_dir))
    with contextlib.suppress(OSError):
        for name in ("owner.json", "bu.sock", "bu.pid"):
            with contextlib.suppress(OSError):
                (Path(runtime_dir) / name).unlink()
        Path(runtime_dir).rmdir()


def _read_daemon_pid(runtime_dir: str) -> Optional[int]:
    p = Path(runtime_dir) / "bu.pid"
    try:
        raw = p.read_text().strip()
        try:
            return int(json.loads(raw)["pid"])
        except Exception:
            return int(raw.split()[0])
    except Exception:
        return None


def _ipc_shutdown(runtime_dir: str, timeout: float = 2.0) -> bool:
    sock = Path(runtime_dir) / "bu.sock"
    if not sock.exists() or not hasattr(socket, "AF_UNIX"):
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(sock))
            s.sendall((json.dumps({"meta": "shutdown"}) + "\n").encode())
            data = b""
            while not data.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        return bool(json.loads(data or b"{}").get("ok"))
    except Exception:
        return False


def stop_owned_daemon(runtime_dir: str, grace_s: float = 3.0) -> dict:
    """Stop the harness daemon living in ``runtime_dir`` — only if ownership is verified."""
    pid = _read_daemon_pid(runtime_dir)
    rep: dict[str, Any] = {"runtime_dir": runtime_dir, "pid": pid, "stopped": False, "reason": ""}
    if pid is None:
        rep["reason"] = "no_pid_file"
        return rep
    if not _alive(pid):
        rep.update(stopped=True, reason="already_dead")
        return rep
    if not daemon_is_ours(pid, runtime_dir):
        rep["reason"] = "not_owned"
        logger.warning("guardrails: refusing to signal pid %s — not started with owned runtime dir %s", pid, runtime_dir)
        return rep
    steps = []
    if _ipc_shutdown(runtime_dir):
        steps.append("ipc_shutdown")
    deadline = time.monotonic() + max(0.2, grace_s)
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _alive(pid):
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(pid, signal.SIGTERM); steps.append("sigterm")
        deadline = time.monotonic() + max(0.2, grace_s)
        while _alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
    if _alive(pid):
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(pid, signal.SIGKILL); steps.append("sigkill")
        deadline = time.monotonic() + 2.0
        while _alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
    rep.update(stopped=not _alive(pid), reason="+".join(steps) or "none")
    for name in ("bu.sock", "bu.pid"):
        with contextlib.suppress(OSError):
            (Path(runtime_dir) / name).unlink()
    return rep


# ---------------------------------------------------------------------------------------------
# resource scope
# ---------------------------------------------------------------------------------------------

def _systemd_run_available() -> bool:
    if os.name == "nt" or not shutil.which("systemd-run"):
        return False
    try:
        r = subprocess.run(["systemctl", "--user", "is-system-running"], capture_output=True, text=True, timeout=5)
        return r.returncode in (0, 1)  # "running" / "degraded" both have a live manager
    except Exception:
        return False


def scope_argv(unit: str, scope: dict) -> list[str]:
    argv = ["systemd-run", "--user", "--scope", "--quiet", "--collect", f"--unit={unit}"]
    if scope.get("slice"):
        argv.append(f"--slice={scope['slice']}")
    for key, prop in (("memory_max", "MemoryMax"), ("memory_high", "MemoryHigh"), ("cpu_weight", "CPUWeight"),
                      ("memory_swap_max", "MemorySwapMax")):
        if scope.get(key) not in (None, ""):
            argv += ["-p", f"{prop}={scope[key]}"]
    argv += ["-p", "OOMPolicy=kill", "--"]
    return argv


def _stop_scope(unit: str) -> bool:
    try:
        r = subprocess.run(["systemctl", "--user", "kill", "--signal=SIGKILL", unit], capture_output=True, timeout=10)
        subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------------------------
# OwnedWork
# ---------------------------------------------------------------------------------------------

class OwnedWork:
    """Everything one guarded call owns: CLI process groups, its daemon runtime dir, its scope."""

    def __init__(self, key: str, *, runtime_dir: Optional[str] = None, scope: Optional[dict] = None):
        global _counter
        with _lock:
            _counter += 1
            n = _counter
        self.key = key
        self.id = f"{os.getpid()}-{n}-{secrets.token_hex(3)}"
        self.pgids: list[int] = []
        self.procs: list = []
        self.runtime_dir: Optional[str] = runtime_dir
        self.scope = dict(scope) if scope else None
        self.scope_unit: Optional[str] = None
        self.scope_applied = False
        self.scope_error = ""
        self.created_at = time.time()
        self.terminated = False
        self.worker: Optional[threading.Thread] = None   # the deadline worker thread (set by deadline.run_with_deadline)
        self.killed_all = False            # a stop_daemon=True terminate ran: nothing owned may survive
        self.cleanup_unconfirmed = False   # finalizer could not verify cleanup → lease NOT released
        self.deferred_finalize: Optional[Any] = None  # finalize fn kept for /guardrails recover when fail-closed
        self.report: dict = {}
        with _lock:
            _registry[self.id] = self

    # -- seam callbacks ------------------------------------------------------------------
    def prepare(self, cmd: list, env: dict) -> list:
        if self.runtime_dir is None:
            self.runtime_dir = runtime_dir_for(self.key)
        env[RUNTIME_ENV] = self.runtime_dir
        marker = _owner_marker(self.runtime_dir)
        if not marker:  # runtime_dir was handed in from elsewhere: register it with this process
            with _lock:
                marker = _markers.setdefault(self.runtime_dir, secrets.token_hex(16))
        env[OWNER_ENV] = marker
        if self.scope and _systemd_run_available():
            # systemd-run --user needs the user bus; upstream's scrubbed env keeps these, but be explicit.
            for k in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
                if not env.get(k) and os.environ.get(k):
                    env[k] = os.environ[k]
            self.scope_unit = f"hg-{hashlib.sha256(self.id.encode()).hexdigest()[:10]}.scope"
            return scope_argv(self.scope_unit, self.scope) + list(cmd)
        if self.scope:
            self.scope_error = "systemd_user_unavailable"
        return list(cmd)

    def spawned(self, proc) -> None:
        self.procs.append(proc)
        self.pgids.append(proc.pid)  # start_new_session=True → pgid == pid
        if self.scope_unit:
            self.scope_applied = True

    # -- lifecycle -----------------------------------------------------------------------
    def alive(self, include_daemon: bool = True) -> list[dict]:
        """Owned things still running. ``include_daemon=False`` (normal completion) leaves the warm
        daemon and its env-marked descendants out; ``True`` counts every env-marked process."""
        out = []
        for pg in self.pgids:
            if _alive(pg):
                out.append({"kind": "pgid", "pid": pg})
        try:
            import psutil
            for pg in self.pgids:
                for p in psutil.process_iter(["pid"]):
                    try:
                        if os.getpgid(p.pid) == pg and _alive(p.pid) and p.pid != pg:
                            out.append({"kind": "group_member", "pid": p.pid, "pgid": pg})
                    except Exception:
                        continue
        except Exception:
            pass
        if include_daemon and self.runtime_dir:
            pid = _read_daemon_pid(self.runtime_dir)
            if pid and daemon_is_ours(pid, self.runtime_dir):
                out.append({"kind": "daemon", "pid": pid})
            seen = {d["pid"] for d in out}
            for mp in marked_processes(self.runtime_dir):
                if mp not in seen:
                    out.append({"kind": "marked", "pid": mp})
        return out

    def kill_groups(self, grace_s: float) -> list[int]:
        killed: list[int] = []
        for pg in self.pgids:
            if not _alive(pg) and not self._group_members(pg):
                continue
            killed.append(pg)
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(pg, signal.SIGTERM)
        deadline = time.monotonic() + max(0.1, grace_s)
        while time.monotonic() < deadline and any(_alive(pg) or self._group_members(pg) for pg in killed):
            time.sleep(0.05)
        for pg in killed:
            if _alive(pg) or self._group_members(pg):
                with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                    os.killpg(pg, signal.SIGKILL)
        deadline = time.monotonic() + max(0.1, grace_s)
        while time.monotonic() < deadline and any(_alive(pg) or self._group_members(pg) for pg in killed):
            time.sleep(0.05)
        for p in self.procs:  # reap
            with contextlib.suppress(Exception):
                p.poll()
        return killed

    @staticmethod
    def _group_members(pg: int) -> list[int]:
        try:
            import psutil
            out = []
            for p in psutil.process_iter(["pid"]):
                try:
                    if os.getpgid(p.pid) == pg and _alive(p.pid):
                        out.append(p.pid)
                except Exception:
                    continue
            return out
        except Exception:
            return []

    def terminate(self, grace_s: float = 5.0, *, stop_daemon: bool = True) -> dict:
        """Stop ALL owned work; verified ownership only. Idempotent."""
        rep: dict[str, Any] = {"pgids_killed": [], "daemon": None, "marked_killed": [], "scope": None}
        rep["pgids_killed"] = self.kill_groups(grace_s)
        others = shared_with_live_work(self.runtime_dir, exclude=self) if (stop_daemon and self.runtime_dir) else []
        if stop_daemon and self.runtime_dir and others:
            # FAIL SAFE: another live owned call of this process is using the same daemon (capacity > 1,
            # or a session-end racing a late-finalizing call). Never signal it from under them: only
            # this call's own process groups die; killed_all stays False so the finalizer verifies
            # just those. The daemon is stopped by whichever owned work closes last / at session end.
            rep["daemon"] = {"runtime_dir": self.runtime_dir, "pid": _read_daemon_pid(self.runtime_dir), "stopped": False,
                             "reason": "in_use_by_other_owned_work", "others": [w.id for w in others]}
            logger.warning("guardrails: not stopping daemon in %s — in use by live owned work %s", self.runtime_dir,
                           [w.id for w in others])
        elif stop_daemon and self.runtime_dir:
            rep["daemon"] = stop_owned_daemon(self.runtime_dir, grace_s=grace_s)
            # startup race / detached children: anything still carrying our exact env pair
            rep["marked_killed"] = kill_marked(self.runtime_dir, grace_s=min(grace_s, 3.0))
            self.killed_all = True
        if self.scope_unit and self.scope_applied:
            rep["scope"] = {"unit": self.scope_unit, "stopped": _stop_scope(self.scope_unit)}
        rep["alive_after"] = self.alive(include_daemon=stop_daemon)
        self.terminated = True
        self.report = rep
        return rep

    def close(self) -> None:
        with _lock:
            _registry.pop(self.id, None)
        # last user of a dir whose session already ended: stop its daemon now (deferred session end)
        with contextlib.suppress(Exception):
            _maybe_stop_ended_dir(self.runtime_dir)


# ---------------------------------------------------------------------------------------------
# context attach + the spawn seam
# ---------------------------------------------------------------------------------------------

@contextlib.contextmanager
def attach(work: OwnedWork):
    token = _CURRENT.set(work)
    try:
        yield work
    finally:
        _CURRENT.reset(token)


def current() -> Optional[OwnedWork]:
    return _CURRENT.get()


def owned_launch(cmd, code, env, timeout, work: "OwnedWork"):
    """Explicit owned launch primitive. Contract (pinned against upstream by
    ``test_owned.py::test_owned_launch_matches_upstream_*``): same Popen kwargs, same
    communicate/timeout/kill/drain sequence, ``CompletedProcess.args == cmd``; on timeout the raised
    ``TimeoutExpired`` carries the UNSCOPED ``cmd`` and no captured output (the scope argv —
    ``systemd-run … -p MemoryMax=…`` — and partial output never reach tool_error text)."""
    from tools import browser_use_cli as BU
    argv = work.prepare(list(cmd), env)
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", env=env, **BU._group_popen_kwargs(),
        )
    except OSError as e:  # never leak the scoped argv / filename into the tool error
        raise OSError(e.errno, f"{e.strerror or type(e).__name__} (owned browser launch)") from None
    work.spawned(proc)
    try:
        stdout, stderr = proc.communicate(input=code, timeout=timeout)
    except subprocess.TimeoutExpired:
        BU._kill_cli_process_group(proc)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.communicate(timeout=BU._POST_KILL_DRAIN_S)
        raise subprocess.TimeoutExpired(list(cmd), timeout) from None
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def _owned_run_cli_killing_process_group(cmd, code, env, timeout):
    """Adapter installed in place of upstream's spawn function: no owned context → upstream
    untouched; owned context → ``owned_launch``."""
    work = _CURRENT.get()
    if work is None:
        original = _installed_original
        if original is None:  # seam not installed through install_spawn_seam (tests): plain upstream copy path
            raise RuntimeError("guardrails: spawn adapter invoked without an installed upstream original")
        return original(cmd, code, env, timeout)
    return owned_launch(cmd, code, env, timeout, work)


def install_spawn_seam() -> bool:
    """Replace upstream's spawn function (module attribute, looked up at call time by browser_exec)."""
    global _installed_original
    try:
        from tools import browser_use_cli as BU
    except Exception:
        logger.warning("guardrails: tools.browser_use_cli unavailable; ownership seam NOT installed")
        return False
    if getattr(BU._run_cli_killing_process_group, "__guardrail_seam__", False):
        return True
    _installed_original = BU._run_cli_killing_process_group
    _owned_run_cli_killing_process_group.__guardrail_seam__ = True  # type: ignore[attr-defined]
    BU._run_cli_killing_process_group = _owned_run_cli_killing_process_group
    return True


def seam_installed() -> bool:
    try:
        from tools import browser_use_cli as BU
        return bool(getattr(BU._run_cli_killing_process_group, "__guardrail_seam__", False))
    except Exception:
        return False


def live_work() -> list[OwnedWork]:
    with _lock:
        return list(_registry.values())


def reset_for_tests() -> None:
    global _installed_original
    with _lock:
        works = list(_registry.values())
        _registry.clear()
    for w in works:
        with contextlib.suppress(Exception):
            w.terminate(grace_s=1.0)
    with _lock:
        dirs = list(_markers)
    for d in dirs:
        with contextlib.suppress(Exception):
            kill_marked(d, grace_s=1.0)
    with _lock:
        _markers.clear()
        _groups.clear()
        _ended_dirs.clear()
    if _installed_original is not None:
        with contextlib.suppress(Exception):
            from tools import browser_use_cli as BU
            BU._run_cli_killing_process_group = _installed_original
        _installed_original = None
