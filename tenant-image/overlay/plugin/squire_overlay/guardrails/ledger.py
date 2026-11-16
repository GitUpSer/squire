"""Harness-owned task/checkpoint ledger (acceptance item 5, 7).

One JSON file per ORIGIN task (``HERMES_HOME/guardrails/ledger/<task_key>.json``), keyed by
the originating chat/session, so every sibling subagent and every tool call that serves the
same user request lands in the same ledger — whichever thread or process ran it. Written
under a flock; readers get a consistent snapshot.

Updated from the tool lifecycle by the plugin, never by the model: it exists even when the
model never writes its promised report. It also carries the structured timing telemetry
(queue/startup/execution/recovery/finalization/delivery) emitted by the browser wrapper.

Self-healing: every in-flight entry records the pid of the process that opened it. A process that
dies (SIGKILL / OOM / crash) never runs ``tool_finished``; ``reconcile`` drops such entries (dead
pid) and marks a ledger left with nothing in flight ``abandoned``, and marks any ledger whose
oldest in-flight entry is older than ``max_age_s`` ``stale``. Both stop the progress notifier —
without this a crashed process would leave a ``running`` ledger that notifies the chat forever.

Task scope: a chat's ledger file lives for days and is reused by every new request, so ``task``
holds the stats of the CURRENT logical task only (started_at, tool_calls, tool_errors, children).
It is reopened whenever the ledger goes from a finished status back to ``running``; progress
notices report from it, never from ``created_at`` / the lifetime ``counters``. The stored
``origin`` is never rewritten on reactivation (a blanked chat_id keeps a chat muted).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .lease import _pid_alive, state_dir

MAX_EVENTS = 400


def _new_task(now: float) -> dict:
    """Per-task scope: the stats a progress notice may honestly report. Reset every time the
    ledger goes from a finished state back to ``running`` (new user request on a reused chat)."""
    return {"started_at": now, "tool_calls": 0, "tool_errors": 0, "children": 0}


def task_scope(d: dict, now: float) -> dict:
    """Current task block of a ledger dict (older ledgers without one fall back to the oldest
    in-flight entry, never to the ledger's ``created_at``)."""
    t = d.get("task")
    if isinstance(t, dict) and t.get("started_at") is not None:
        return t
    inflight = (d.get("in_flight") or {}).values()
    start = min((float(e.get("started_at", now)) for e in inflight), default=now)
    return {"started_at": start, "tool_calls": 0, "tool_errors": 0, "children": 0}


def task_key(origin: dict) -> str:
    base = origin.get("session_key") or f"{origin.get('platform', '')}:{origin.get('chat_id', '')}"
    return re.sub(r"[^A-Za-z0-9_.@-]", "_", str(base))[:120] or "unknown"


def ledger_dir() -> Path:
    return state_dir() / "ledger"


def _redact(text: str) -> str:
    try:
        from agent.redact import redact_sensitive_text
        text = redact_sensitive_text(text, force=True)
    except Exception:
        pass
    return re.sub(r"(?i)(bearer|token|authorization|api[_-]?key|password)[:= ]+\S+", r"\1=[redacted]", text)[:300]


class Ledger:
    def __init__(self, now: Callable[[], float] = time.time):
        self.now = now

    # -- storage --------------------------------------------------------------------------
    def _path(self, origin: dict) -> Path:
        d = ledger_dir()
        d.mkdir(parents=True, exist_ok=True)
        return d / (task_key(origin) + ".json")

    @contextlib.contextmanager
    def _locked(self, origin: dict):
        path = self._path(origin)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            raw = b""
            while True:
                chunk = os.read(fd, 1 << 16)
                if not chunk:
                    break
                raw += chunk
            try:
                data = json.loads(raw.decode("utf-8")) if raw.strip() else {}
            except Exception:
                data = {}
            if not data:
                data = {"origin": {k: origin.get(k, "") for k in ("platform", "chat_id", "session_key")},
                        "created_at": self.now(), "status": "running", "in_flight": {}, "children": {},
                        "events": [], "blockers": {}, "notifications": [], "telemetry": [],
                        "counters": {"tool_calls": 0, "tool_errors": 0, "children": 0},
                        "task": _new_task(self.now())}
            data["updated_at"] = self.now()
            yield data
            data["events"] = data["events"][-MAX_EVENTS:]
            payload = json.dumps(data, sort_keys=True, default=str).encode("utf-8")
            os.lseek(fd, 0, 0)
            os.ftruncate(fd, 0)
            os.write(fd, payload)
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def snapshot(self, origin: dict) -> dict:
        with self._locked(origin) as d:
            return json.loads(json.dumps(d))

    def list_active(self) -> list[dict]:
        d = ledger_dir()
        out = []
        if not d.exists():
            return out
        for p in sorted(d.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if data.get("status") == "running":
                out.append(data)
        return out

    def _activate(self, d: dict) -> None:
        """Mark the ledger running; if it was finished (completed/failed/interrupted) or its process
        died (``abandoned``), this is a NEW logical task: open a fresh task scope so timers/counts
        never carry over. A ledger retired for AGE (``stale``) is the SAME task still running past
        ``notify_max_age_s``: its scope is kept, so reactivation can neither restart the elapsed clock
        ("1m elapsed" for a 2-hour task) nor the max-age bound (``reconcile_data`` re-retires it at
        once because it also looks at ``task.started_at``). ``origin`` is deliberately left as stored
        (a blanked chat_id = muted chat stays muted)."""
        if d.get("status") not in ("running", "stale") or not isinstance(d.get("task"), dict):
            d["task"] = _new_task(self.now())
        d["status"] = "running"
        d["finished_at"] = None

    # -- tool lifecycle -------------------------------------------------------------------
    def tool_started(self, origin: dict, *, task_id: str, tool_call_id: str, tool_name: str, session_id: str = "") -> None:
        with self._locked(origin) as d:
            self._activate(d)
            d["in_flight"][tool_call_id or f"anon-{len(d['in_flight'])}"] = {
                "tool": tool_name, "task_id": task_id, "session_id": session_id, "started_at": self.now(),
                "pid": os.getpid()}
            d["counters"]["tool_calls"] += 1
            d["task"]["tool_calls"] += 1
            d["events"].append({"at": self.now(), "kind": "tool_start", "tool": tool_name, "task_id": task_id,
                                "tool_call_id": tool_call_id})

    def tool_finished(self, origin: dict, *, tool_call_id: str, status: str, reason: str = "",
                      duration_s: float = 0.0, telemetry: Optional[dict] = None) -> None:
        with self._locked(origin) as d:
            entry = d["in_flight"].pop(tool_call_id, None) or {}
            if status != "ok":
                d["counters"]["tool_errors"] += 1
                if isinstance(d.get("task"), dict):
                    d["task"]["tool_errors"] = int(d["task"].get("tool_errors", 0)) + 1
            d["events"].append({"at": self.now(), "kind": "tool_end", "tool": entry.get("tool", ""), "status": status,
                                "reason": reason, "duration_s": duration_s, "task_id": entry.get("task_id", ""),
                                "tool_call_id": tool_call_id})
            d["blockers"].pop(entry.get("task_id", ""), None)
            if telemetry:
                d["telemetry"] = (d.get("telemetry") or [])[-99:] + [dict(telemetry, at=self.now(), tool_call_id=tool_call_id)]

    def set_blocker(self, origin: dict, *, task_id: str, code: str, detail: str = "") -> None:
        with self._locked(origin) as d:
            d["blockers"][task_id] = {"code": code, "detail": _redact(detail), "since": self.now()}

    # -- turn lifecycle (dispatch-time entry; progress visible while the MODEL hangs) -------
    def turn_started(self, origin: dict, *, task_id: str, turn_id: str, session_id: str = "", marker: str = "llm:turn") -> None:
        key = f"turn:{turn_id or task_id}"
        with self._locked(origin) as d:
            self._activate(d)
            d["in_flight"].setdefault(key, {"tool": marker, "task_id": task_id, "session_id": session_id,
                                            "started_at": self.now(), "pid": os.getpid()})
            d["events"].append({"at": self.now(), "kind": "turn_start", "task_id": task_id, "turn_id": turn_id})

    def turn_finished(self, origin: dict, *, turn_id: str, marker: str = "llm:turn") -> None:
        with self._locked(origin) as d:
            if turn_id:
                d["in_flight"].pop(f"turn:{turn_id}", None)
            else:
                for k in [k for k, v in d["in_flight"].items() if v.get("tool") == marker]:
                    d["in_flight"].pop(k, None)
            d["events"].append({"at": self.now(), "kind": "turn_end", "turn_id": turn_id})

    def clear_blocker(self, origin: dict, *, task_id: str) -> None:
        with self._locked(origin) as d:
            d["blockers"].pop(task_id, None)

    # -- children -------------------------------------------------------------------------
    def child_started(self, origin: dict, *, parent_task_id: str, child_task_id: str, goal: str = "") -> None:
        with self._locked(origin) as d:
            self._activate(d)
            d["children"][child_task_id] = {"parent": parent_task_id, "goal": _redact(goal)[:120], "status": "running",
                                            "started_at": self.now()}
            d["counters"]["children"] += 1
            d["task"]["children"] += 1
            d["events"].append({"at": self.now(), "kind": "child_start", "task_id": child_task_id})

    def child_finished(self, origin: dict, *, child_task_id: str, status: str, summary: str = "") -> None:
        with self._locked(origin) as d:
            c = d["children"].setdefault(child_task_id, {"parent": "", "goal": "", "started_at": self.now()})
            c.update({"status": status, "finished_at": self.now(), "summary": _redact(summary)[:200]})
            d["events"].append({"at": self.now(), "kind": "child_end", "task_id": child_task_id, "status": status})
            stale = [k for k, v in d["in_flight"].items() if v.get("task_id") == child_task_id]
            for k in stale:
                d["in_flight"].pop(k, None)
            d["blockers"].pop(child_task_id, None)

    # -- task -----------------------------------------------------------------------------
    def task_finished(self, origin: dict, *, task_id: str, status: str) -> None:
        with self._locked(origin) as d:
            d["status"] = status
            d["finished_at"] = self.now()
            d["in_flight"] = {}
            d["blockers"] = {}
            d["events"].append({"at": self.now(), "kind": "task_end", "task_id": task_id, "status": status})

    # -- self-healing ---------------------------------------------------------------------
    @staticmethod
    def reconcile_data(d: dict, now: float, *, max_age_s: float) -> Optional[str]:
        """Apply crash/staleness hygiene to one ledger dict IN PLACE (caller holds the lock).
        Returns the new status when it changed ("abandoned" / "stale"), else None."""
        if d.get("status") != "running":
            return None
        inflight = d.get("in_flight") or {}
        dead = [k for k, e in inflight.items() if e.get("pid") and not _pid_alive(int(e.get("pid") or 0))]
        for k in dead:
            e = inflight.pop(k)
            d["events"].append({"at": now, "kind": "tool_end", "tool": e.get("tool", ""), "status": "abandoned",
                                "reason": "process_gone", "task_id": e.get("task_id", ""), "tool_call_id": k})
            d["blockers"].pop(e.get("task_id", ""), None)
        if dead and not inflight:
            d.update({"status": "abandoned", "finished_at": now, "blockers": {}})
            d["events"].append({"at": now, "kind": "task_end", "status": "abandoned", "reason": "process_gone"})
            return "abandoned"
        if inflight and max_age_s > 0:
            oldest = min(float(e.get("started_at", now)) for e in inflight.values())
            task = d.get("task")
            if isinstance(task, dict) and task.get("started_at") is not None:
                # the task's own start counts too: a task reactivated after a `stale` retirement
                # (scope kept by _activate) is retired again here instead of notifying for another
                # max_age_s window
                oldest = min(oldest, float(task["started_at"]))
            if now - oldest > max_age_s:
                d.update({"status": "stale", "finished_at": now, "in_flight": {}, "blockers": {}})
                d["events"].append({"at": now, "kind": "task_end", "status": "stale", "reason": "max_age"})
                return "stale"
        return None

    def reconcile_all(self, *, max_age_s: float) -> list[tuple[str, str]]:
        """Startup / periodic sweep over every running ledger. Returns [(task_key, new_status)]."""
        out: list[tuple[str, str]] = []
        for data in self.list_active():
            origin = data.get("origin") or {}
            with self._locked(origin) as d:
                changed = self.reconcile_data(d, self.now(), max_age_s=max_age_s)
            if changed:
                out.append((task_key(origin), changed))
        return out

    def record_notification(self, origin: dict, *, text: str, delivered: bool, error: str = "") -> None:
        with self._locked(origin) as d:
            d["notifications"] = (d.get("notifications") or [])[-19:] + [
                {"at": self.now(), "delivered": delivered, "error": _redact(error), "chars": len(text)}]
