"""Infrastructure circuit breaker (acceptance item 3).

Two layers, both persisted in ``HERMES_HOME/guardrails/breaker/<dependency>.json`` so every
process / session name on the host sees the same state:

* **per task** — ``consecutive`` infra failures (opens at 2) and ``spent_s`` cumulative
  recovery cost (opens at ``recovery_budget_s``, default 120 s). The task key is the origin
  chat's STABLE session key, so this state must be able to heal on its own or one chat would be
  locked out of the browser forever. It heals three ways: a SUCCESSFUL call resets both counters
  (the infrastructure demonstrably works again — a new failure streak starts a new budget); a
  ROLLING WINDOW (``reset_after_s``, default 30 min since the last infra failure) forgets the
  task state entirely; and ``/guardrails recover`` (``clear_task_state``) wipes it explicitly.
* **host health** — per dependency: after a task trips, the dependency is marked unhealthy
  for ``retry_after_s`` (exponential, 60 s → 120 → 240 … capped). After cooldown exactly ONE
  caller is admitted as a half-open probe; its success closes the dependency, its failure
  reopens with a longer backoff. Sibling tasks are told ``dependency_unhealthy_host`` /
  ``dependency_half_open_probe_in_flight`` without burning their own budget.

``classify`` decides what counts as infrastructure. Retailer 404s, DOM/validation errors,
login redirects and bad arguments are NOT infra: retrying them is the model's decision, not
ours, and they must never open the breaker. ``retry_policy`` refuses auto-retry when the
payload could be a write / auth / payment action.

Detail strings stored on disk go through Hermes' redaction, and only the first 200 chars.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .lease import state_dir

CONSECUTIVE_LIMIT = 2
DEFAULT_RECOVERY_BUDGET_S = 120.0
BASE_BACKOFF_S = 60.0
MAX_BACKOFF_S = 900.0
PROBE_TTL_S = 300.0  # a half-open probe that never reports back expires
# Per-task state (consecutive / spent_s) is forgotten this long after the task's LAST infra
# failure. Bounds the worst case lockout of one chat to the window; configurable via the
# `breaker_reset_after_s` guardrail setting.
DEFAULT_RESET_AFTER_S = 1800.0

_INFRA_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("infra_cli_missing", re.compile(r"browser-use CLI not found|uvx is unavailable", re.I)),
    ("infra_launch_failed", re.compile(r"Failed to launch browser-use CLI|failed to (start|launch) (chrom|browser)", re.I)),
    ("infra_timeout", re.compile(r"exec timed out after \d+s|deadline_exceeded|TimeoutExpired", re.I)),
    ("infra_cdp_unreachable", re.compile(r"CDP connection (refused|failed|lost)|ECONNREFUSED 127\.0\.0\.1:\d+|"
                                         r"websocket.*(closed|refused)|browser (daemon|harness) (unavailable|not running|crashed)", re.I)),
    ("infra_resource_pressure", re.compile(r"Cannot allocate memory|Out of memory|OOM|Resource temporarily unavailable", re.I)),
)

_WRITE_HINTS = re.compile(
    r"\.click\(|click_at_xy\(|\.submit\(|fill_input\(|type_text\(|press\(|checkout|purchase|add[_ -]?to[_ -]?cart|"
    r"place[_ -]?order|\bbuy\b|payment|confirm|delete|send\b", re.I)
_AUTH_TOOLS = frozenset({"browser_vault_fill", "browser_vault_enter_code", "browser_vault_save_login", "browser_vault_unlock"})


def classify(result: Any) -> Optional[str]:
    """Return an ``infra_*`` reason code when a tool result indicates an infrastructure outage."""
    if result is None:
        return None
    text = result if isinstance(result, str) else json.dumps(result, default=str)
    try:
        obj = json.loads(text) if isinstance(text, str) and text.lstrip().startswith("{") else None
    except Exception:
        obj = None
    if isinstance(obj, dict):
        # Only an *error* field or a failed exec can be infra; a successful run with site text is not.
        if obj.get("success") is True:
            return None
        candidates = [str(obj.get("error") or ""), str(obj.get("stderr") or "")]
        if obj.get("success") is False:
            candidates.append(str(obj.get("output") or "")[-2000:])
        text = "\n".join(candidates)
    for code, rx in _INFRA_PATTERNS:
        if rx.search(text):
            return code
    return None


def retry_policy(tool_name: str, args: dict, infra_reason: Optional[str]) -> str:
    if infra_reason is None:
        return "not_infra"
    if tool_name in _AUTH_TOOLS:
        return "no_retry_auth"
    code = str((args or {}).get("code") or "")
    if _WRITE_HINTS.search(code):
        return "no_retry_uncertain_write"
    return "retry_allowed"


def _redact(s: str) -> str:
    try:
        from agent.redact import redact_sensitive_text
        s = redact_sensitive_text(s, force=True)
    except Exception:
        pass
    s = re.sub(r"(?i)(bearer|token|authorization|api[_-]?key|password)[:= ]+\S+", r"\1=[redacted]", s)
    return s[:200]


@dataclass
class Verdict:
    allowed: bool
    reason: str = "closed"
    failures: int = 0
    spent_s: float = 0.0
    retry_after_s: float = 0.0
    half_open: bool = False
    holder: Optional[str] = None
    resets_in_s: float = 0.0      # for task-open verdicts: seconds until the rolling window forgets the state

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in (None, 0, 0.0, False) or k in ("allowed", "reason")}


class _Store:
    def __init__(self, dependency: str):
        d = state_dir() / "breaker"
        d.mkdir(parents=True, exist_ok=True)
        self.path = d / f"{re.sub(r'[^A-Za-z0-9_-]', '_', dependency)[:60]}.json"

    @contextlib.contextmanager
    def locked(self):
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            raw = b""
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                raw += chunk
            try:
                data = json.loads(raw.decode("utf-8")) if raw.strip() else {}
            except Exception:
                data = {}
            data.setdefault("tasks", {})
            data.setdefault("host", {"state": "closed", "open_until": 0.0, "backoff_s": 0.0, "probe": None, "trips": 0})
            yield data
            payload = json.dumps(data, sort_keys=True).encode("utf-8")
            os.lseek(fd, 0, 0)
            os.ftruncate(fd, 0)
            os.write(fd, payload)
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def read(self) -> dict:
        with self.locked() as d:
            return json.loads(json.dumps(d))


class Breaker:
    def __init__(self, dependency: str, *, task_id: str, now: Callable[[], float] = time.time,
                 recovery_budget_s: float = DEFAULT_RECOVERY_BUDGET_S, consecutive_limit: int = CONSECUTIVE_LIMIT,
                 reset_after_s: float = DEFAULT_RESET_AFTER_S):
        self.dependency = dependency
        self.task_id = task_id or "default"
        self.now = now
        self.budget = float(recovery_budget_s)
        self.limit = int(consecutive_limit)
        self.reset_after = float(reset_after_s)
        self.store = _Store(dependency)
        self._probe_token: Optional[str] = None

    # -- host health ----------------------------------------------------------------------
    def _host_verdict(self, host: dict) -> Verdict:
        t = self.now()
        state = host.get("state", "closed")
        if state == "closed":
            return Verdict(True)
        probe = host.get("probe")
        if probe and t - float(probe.get("at", 0)) > PROBE_TTL_S:
            host["probe"] = probe = None
        if state == "open":
            if t < float(host.get("open_until", 0)):
                return Verdict(False, "dependency_unhealthy_host", retry_after_s=round(float(host["open_until"]) - t, 1))
            # cooldown elapsed → admit THIS caller as the probe
            host["state"] = "half_open"
            host["probe"] = {"task": self.task_id, "at": t}
            self._probe_token = self.task_id
            return Verdict(True, "half_open_probe", half_open=True)
        # half_open
        if probe and probe.get("task") == self.task_id:
            return Verdict(True, "half_open_probe", half_open=True)
        if probe:
            return Verdict(False, "dependency_half_open_probe_in_flight", holder=probe.get("task"),
                           retry_after_s=round(PROBE_TTL_S - (t - float(probe.get("at", 0))), 1))
        host["probe"] = {"task": self.task_id, "at": t}
        return Verdict(True, "half_open_probe", half_open=True)

    def _task_state(self, d: dict) -> dict:
        """This task's record, after applying the rolling window: once ``reset_after_s`` has passed
        since the LAST infra failure, consecutive/spent_s are forgotten (the record stays for the
        history/status view). Called under the store lock; mutates ``d`` in place."""
        task = d["tasks"].get(self.task_id)
        if not task:
            return {}
        last = float(task.get("last_failure_at", 0))
        if (task.get("consecutive") or task.get("spent_s")) and self.reset_after > 0 \
                and self.now() - last > self.reset_after:
            task.update({"consecutive": 0, "spent_s": 0.0, "reset_at": self.now(), "reset_reason": "window"})
        return task

    def _resets_in(self, task: dict) -> float:
        if self.reset_after <= 0:
            return 0.0
        return round(max(0.0, self.reset_after - (self.now() - float(task.get("last_failure_at", 0)))), 1)

    def _trip_host(self, host: dict) -> None:
        trips = int(host.get("trips", 0)) + 1
        backoff = min(MAX_BACKOFF_S, BASE_BACKOFF_S * (2 ** (trips - 1)))
        host.update({"state": "open", "open_until": self.now() + backoff, "backoff_s": backoff,
                     "probe": None, "trips": trips, "last_trip_at": self.now()})

    # -- public ---------------------------------------------------------------------------
    def check(self) -> Verdict:
        with self.store.locked() as d:
            task = self._task_state(d)
            if int(task.get("consecutive", 0)) >= self.limit:
                return Verdict(False, "breaker_open_consecutive_failures", failures=int(task["consecutive"]),
                               spent_s=float(task.get("spent_s", 0)), resets_in_s=self._resets_in(task))
            if float(task.get("spent_s", 0)) >= self.budget:
                return Verdict(False, "breaker_open_recovery_budget", failures=int(task.get("consecutive", 0)),
                               spent_s=float(task["spent_s"]), resets_in_s=self._resets_in(task))
            v = self._host_verdict(d["host"])
            v.failures = int(task.get("consecutive", 0))
            v.spent_s = float(task.get("spent_s", 0))
            return v

    def record_failure(self, reason: str, *, cost_s: float, detail: str = "") -> Verdict:
        with self.store.locked() as d:
            task = d["tasks"].setdefault(self.task_id, {"consecutive": 0, "spent_s": 0.0, "history": []})
            self._task_state(d)   # apply the window BEFORE adding, so an old streak does not carry over
            task["consecutive"] = int(task.get("consecutive", 0)) + 1
            task["spent_s"] = float(task.get("spent_s", 0)) + float(cost_s)
            task["last_failure_at"] = self.now()
            task["history"] = (task.get("history") or [])[-9:] + [
                {"at": self.now(), "reason": reason, "cost_s": float(cost_s), "detail": _redact(detail)}]
            host = d["host"]
            tripped = task["consecutive"] >= self.limit or task["spent_s"] >= self.budget
            probe = host.get("probe")
            if host.get("state") == "half_open" and probe and probe.get("task") == self.task_id:
                self._trip_host(host)      # probe failed → reopen, longer backoff
            elif tripped and host.get("state") == "closed":
                self._trip_host(host)
            return Verdict(not tripped, "recorded", failures=task["consecutive"], spent_s=task["spent_s"])

    def record_success(self) -> None:
        """A successful call proves the dependency works for this task: the failure streak AND the
        recovery budget start over (the budget bounds one streak of recovery attempts, not a whole
        chat's lifetime — the task key is the chat's stable session key)."""
        with self.store.locked() as d:
            task = d["tasks"].setdefault(self.task_id, {"consecutive": 0, "spent_s": 0.0, "history": []})
            task["consecutive"] = 0
            task["spent_s"] = 0.0
            task["last_success_at"] = self.now()
            host = d["host"]
            probe = host.get("probe")
            if host.get("state") == "half_open" and (not probe or probe.get("task") == self.task_id):
                host.update({"state": "closed", "probe": None, "open_until": 0.0, "trips": 0, "backoff_s": 0.0})


    def release_probe(self) -> bool:
        """Give the half-open probe slot back when THIS task claimed it in ``check()`` but the call was
        never admitted (preflight refusal, lease timeout, wrapper exception). Without this the slot
        would stay occupied for PROBE_TTL_S and every sibling would be refused with
        ``dependency_half_open_probe_in_flight`` although nothing is probing."""
        with self.store.locked() as d:
            host = d["host"]
            probe = host.get("probe")
            if host.get("state") == "half_open" and probe and probe.get("task") == self.task_id:
                host["probe"] = None
                self._probe_token = None
                return True
        return False


def clear_task_state(dependency: Optional[str] = None) -> dict:
    """Operator reset (``/guardrails recover``): forget every per-task record and close the host
    circuit for ``dependency`` (all dependencies when None). Returns {dependency: tasks_cleared}."""
    out: dict[str, int] = {}
    for dep in ([dependency] if dependency else list_dependencies()):
        with _Store(dep).locked() as d:
            out[dep] = len(d.get("tasks") or {})
            d["tasks"] = {}
            d["host"] = {"state": "closed", "open_until": 0.0, "backoff_s": 0.0, "probe": None, "trips": 0,
                         "cleared_at": time.time()}
    return out


def snapshot(dependency: str) -> dict:
    return _Store(dependency).read()


def list_dependencies() -> list[str]:
    d = state_dir() / "breaker"
    return sorted(p.stem for p in d.glob("*.json")) if d.exists() else []
