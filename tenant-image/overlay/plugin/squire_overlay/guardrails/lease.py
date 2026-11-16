"""Host-wide resource leases backed by ``fcntl.flock`` (acceptance item 1).

Why flock and not a semaphore/pidfile: the lock is released by the kernel when the holding
process dies for ANY reason (SIGKILL, OOM, crash), so there is never a stale lock to clean up,
and it works across every Hermes process on the host (gateway, CLI, cron, subagent workers)
without a coordinator. A lease of capacity N is N slot files; a holder owns exactly one slot.

Slot files additionally carry a small JSON holder record (owner, pid, started, meta) so
operators and sibling tasks can see WHO holds the browser. The record is advisory; the flock
is the truth. A record left by a dead process is reported as ``recovered_from_crash``.

Queueing: waiters poll with a short sleep (bounded by ``wait_s``). ``max_queue`` bounds how many
waiters may be parked per lease on this host, so a burst of callers fails fast with
``lease_queue_full`` instead of piling up. The waiter count is NOT a counter file: each waiter
holds its own flock'ed ticket file under ``<lease>.q/`` and the count is the number of tickets
whose flock is still held. A waiter that dies (SIGKILL, OOM) loses its flock in the kernel and its
ticket is reaped on the next count — the same self-healing property the slots have. A plain
integer would have stayed inflated forever. ``<lease>.queue`` is only the mutex around
count-and-enter; ``prune_queue`` / ``queue_depth`` expose the state to ``/guardrails``.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

_POLL_S = 0.05
_SAFE = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")).expanduser()


def state_dir() -> Path:
    return _hermes_home() / "guardrails"


def lease_dir() -> Path:
    return state_dir() / "leases"


def _safe(name: str) -> str:
    out = "".join(c if c in _SAFE else "_" for c in (name or ""))
    return out[:80] or "lease"


def lease_name_for_browser(session: str = "", **_: Any) -> str:
    """Interactive browsers share ONE host-wide lease, whatever the session label says.
    Separate session names did not isolate CPU/RAM in the incident; they must not bypass the lease."""
    return "browser"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass
class Lease:
    name: str
    owner: str
    acquired: bool
    reason: str
    holder: Optional[str] = None
    waited_s: float = 0.0
    recovered_from_crash: bool = False
    slot: int = -1
    _fd: Optional[int] = field(default=None, repr=False)
    _path: Optional[Path] = field(default=None, repr=False)

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        with contextlib.suppress(OSError):
            os.ftruncate(fd, 0)
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)
        self.acquired = False

    def __enter__(self) -> "Lease":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def _read_record(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8").strip()
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def _try_slot(path: Path, owner: str, meta: Optional[dict]) -> tuple[Optional[int], bool, Optional[str]]:
    """Try one slot. Returns (fd or None, recovered_from_crash, current_holder_owner)."""
    stale_owner = None
    prior = _read_record(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        os.close(fd)
        return None, False, prior.get("owner") or None
    recovered = bool(prior.get("pid")) and not _pid_alive(int(prior.get("pid") or 0))
    if prior.get("pid") and _pid_alive(int(prior["pid"])) and prior.get("owner"):
        # Lock was free but record claims a live pid: that process released without clearing
        # (or is us re-entering). Not a crash; just overwrite.
        stale_owner = prior.get("owner")
    rec = {"owner": owner, "pid": os.getpid(), "started": time.time(), "meta": meta or {}}
    os.ftruncate(fd, 0)
    os.write(fd, json.dumps(rec).encode("utf-8"))
    os.fsync(fd) if hasattr(os, "fsync") else None
    return fd, recovered, stale_owner


def _queue_dir(d: Path, name: str) -> Path:
    q = d / f"{name}.q"
    q.mkdir(parents=True, exist_ok=True)
    return q


def _live_tickets(qdir: Path, *, prune: bool = True) -> int:
    """Count ticket files whose flock is still held by a live waiter. A ticket whose flock can be
    taken belongs to a dead (or already departed) waiter: it is unlinked when ``prune`` is set."""
    live = 0
    for t in qdir.glob("*.w"):
        try:
            fd = os.open(t, os.O_RDWR)
        except OSError:
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                live += 1          # held → live waiter
                continue
            fcntl.flock(fd, fcntl.LOCK_UN)   # free → stale ticket
            if prune:
                with contextlib.suppress(OSError):
                    t.unlink()
        finally:
            os.close(fd)
    return live


class _QueueTicket:
    """One waiter's place in the queue: a per-waiter file whose flock is held for as long as the
    waiter lives (kernel-released on death). ``<lease>.queue`` is the mutex for count-and-enter so
    two simultaneous enterers cannot both squeeze under ``max_queue``."""

    def __init__(self, d: Path, name: str, max_queue: int):
        self.qdir, self.mutex, self.max_queue = _queue_dir(d, name), d / f"{name}.queue", max_queue
        self.fd: Optional[int] = None
        self.path: Optional[Path] = None

    @property
    def held(self) -> bool:
        return self.fd is not None

    def enter(self) -> bool:
        mfd = os.open(self.mutex, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(mfd, fcntl.LOCK_EX)
            if _live_tickets(self.qdir) >= self.max_queue:
                return False
            path = self.qdir / f"{os.getpid()}-{secrets.token_hex(4)}.w"
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)   # our own fresh file: cannot be contended
            except (BlockingIOError, OSError):
                os.close(fd)
                return False
            with contextlib.suppress(OSError):
                os.write(fd, json.dumps({"pid": os.getpid(), "since": time.time()}).encode("utf-8"))
            self.fd, self.path = fd, path
            return True
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(mfd, fcntl.LOCK_UN)
            os.close(mfd)

    def leave(self) -> None:
        fd, self.fd = self.fd, None
        if fd is None:
            return
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)
        if self.path is not None:
            with contextlib.suppress(OSError):
                self.path.unlink()


def queue_depth(name: str) -> int:
    """Live waiters parked on ``name`` right now (stale tickets of dead waiters are pruned)."""
    name = _safe(name)
    d = lease_dir()
    if not (d / f"{name}.q").exists():
        return 0
    return _live_tickets(_queue_dir(d, name))


def prune_queue(name: str) -> dict:
    """Operator hygiene (``/guardrails recover``): drop stale tickets, report live/stale counts."""
    name = _safe(name)
    d = lease_dir()
    if not (d / f"{name}.q").exists():
        return {"live": 0, "pruned": 0}
    q = _queue_dir(d, name)
    before = len(list(q.glob("*.w")))
    live = _live_tickets(q)
    return {"live": live, "pruned": max(0, before - live)}


def acquire(name: str, *, owner: str, wait_s: float = 0.0, capacity: int = 1,
            max_queue: int = 8, meta: Optional[dict] = None) -> Lease:
    """Acquire one slot of lease ``name``. Never raises for contention; inspect ``.acquired``/``.reason``."""
    name = _safe(name)
    d = lease_dir()
    d.mkdir(parents=True, exist_ok=True)
    capacity = max(1, int(capacity))
    deadline = time.monotonic() + max(0.0, float(wait_s))
    t0 = time.monotonic()
    holder: Optional[str] = None
    ticket: Optional[_QueueTicket] = None
    try:
        while True:
            for i in range(capacity):
                fd, recovered, _ = _try_slot(d / f"{name}.{i}.slot", owner, meta)
                if fd is not None:
                    return Lease(name=name, owner=owner, acquired=True, reason="acquired",
                                 waited_s=time.monotonic() - t0, recovered_from_crash=recovered,
                                 slot=i, _fd=fd, _path=d / f"{name}.{i}.slot")
            if holder is None:
                hs = holders(name, capacity=capacity)
                holder = hs[0]["owner"] if hs else None
            if ticket is None:
                ticket = _QueueTicket(d, name, max_queue)
                if not ticket.enter():
                    return Lease(name=name, owner=owner, acquired=False, reason="lease_queue_full",
                                 holder=holder, waited_s=time.monotonic() - t0)
            if time.monotonic() >= deadline:
                hs = holders(name, capacity=capacity)
                return Lease(name=name, owner=owner, acquired=False, reason="lease_wait_timeout",
                             holder=(hs[0]["owner"] if hs else holder), waited_s=time.monotonic() - t0)
            time.sleep(min(_POLL_S, max(0.0, deadline - time.monotonic())) or _POLL_S)
    finally:
        if ticket is not None:
            ticket.leave()


def holders(name: str, capacity: int = 1) -> list[dict]:
    """Advisory holder records for slots whose flock is currently held."""
    name = _safe(name)
    d = lease_dir()
    out: list[dict] = []
    for i in range(max(1, capacity)):
        p = d / f"{name}.{i}.slot"
        if not p.exists():
            continue
        try:
            fd = os.open(p, os.O_RDWR)
        except OSError:
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                rec = _read_record(p)
                if rec:
                    rec["slot"] = i
                    out.append(rec)
                continue
            fcntl.flock(fd, fcntl.LOCK_UN)  # it was free
        finally:
            os.close(fd)
    return out


# ---- late-finalization bookkeeping (operator visibility; the flock is still the truth) ------------

def _pending_path() -> Path:
    d = state_dir() / "pending"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{_safe(str(os.getpid()))}.json"


def note_pending(owner: str, *, tool_call_id: str, work_id: str) -> None:
    """Record that a lease held by THIS process awaits late finalization (worker still inside upstream)."""
    p = _pending_path()
    data = _read_record(p)
    data.setdefault("pending", {})[tool_call_id] = {"owner": owner, "work_id": work_id, "pid": os.getpid(),
                                                    "since": time.time()}
    with contextlib.suppress(OSError):
        p.write_text(json.dumps(data))


def note_finalized(owner: str) -> None:
    p = _pending_path()
    data = _read_record(p)
    pend = data.get("pending") or {}
    for k in [k for k, v in pend.items() if v.get("owner") == owner]:
        pend.pop(k, None)
    data["pending"] = pend
    with contextlib.suppress(OSError):
        p.write_text(json.dumps(data))


def pending_finalizations() -> list[dict]:
    """Late finalizations still outstanding on this host. A record whose pid is dead is stale by
    definition: the kernel already dropped that process's flock, so nothing is actually held."""
    out = []
    d = state_dir() / "pending"
    if not d.exists():
        return out
    for p in d.glob("*.json"):
        data = _read_record(p)
        for k, v in (data.get("pending") or {}).items():
            out.append(dict(v, tool_call_id=k, process_alive=_pid_alive(int(v.get("pid") or 0))))
    return out


def clear_pending_for_pid(pid: int) -> None:
    """Remove the pending file of a DEAD process only (its flocks are gone with it)."""
    if _pid_alive(pid):
        return
    with contextlib.suppress(OSError):
        (state_dir() / "pending" / f"{_safe(str(pid))}.json").unlink()
