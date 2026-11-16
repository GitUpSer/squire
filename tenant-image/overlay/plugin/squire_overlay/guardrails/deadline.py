"""Absolute monotonic deadline + ownership-exact cancellation + late finalization (blockers 1, 2, 3).

* ``deadline_at`` is ONE absolute ``time.monotonic()`` instant computed by the policy from the
  moment the call entered the middleware (t0): queueing, preflight, CLI startup, daemon reconnect,
  execution and cleanup all live inside it. An already-expired deadline is NOT floored: the
  downstream call is not started at all.
* When the deadline fires, ONLY the ``OwnedWork`` attached to this call is terminated (process
  groups registered through the real spawn seam, the owned harness daemon, the call's resource
  scope). No cmdline heuristics, no descendants-of-the-gateway scans.
* ``cleanup_reserve_s`` bounds how long we wait for the worker thread after the kill. If upstream
  still has not returned (e.g. an unkillable pipe holder), we return ``worker_finished=False`` and
  hand ``finalize`` to the worker thread itself: it runs when the worker eventually returns and
  the owned work is verified dead (``late finalization``) — never while owned execution continues,
  and without needing a restart. The lease is therefore released exactly once, by whichever side
  finishes last.
"""

from __future__ import annotations

import ast
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import owned as OW

logger = logging.getLogger(__name__)

DEFAULT_CLEANUP_RESERVE_S = 10.0


@dataclass
class DeadlineOutcome:
    reason: str                       # "ok" | "deadline_exceeded" | "worker_error"
    result: Any = None
    error: Optional[BaseException] = None
    killed_pids: list[int] = field(default_factory=list)
    worker_finished: bool = True
    overrun_s: float = 0.0
    phases: dict = field(default_factory=dict)
    owned_report: dict = field(default_factory=dict)
    finalized: bool = False           # finalize() already ran (False => deferred to late finalization)


def precheck_code(code: str) -> Optional[dict]:
    """Return a structured error if ``code`` does not even parse; None when it does."""
    try:
        ast.parse(code or "")
    except SyntaxError as e:
        return {"reason": "code_syntax_error", "line": e.lineno or 0, "offset": e.offset or 0,
                "message": f"{e.msg} (line {e.lineno})"}
    except Exception as e:  # pragma: no cover - defensive
        return {"reason": "code_syntax_error", "line": 0, "offset": 0, "message": str(e)[:200]}
    return None


class _Finalizer:
    """Runs ``fn`` exactly once, from whichever side (caller / worker thread) gets there last."""

    def __init__(self, fn: Optional[Callable[[], None]], work: Optional[OW.OwnedWork]):
        self.fn, self.work = fn, work
        self._lock = threading.Lock()
        self.done = False
        self.ran = False

    def run(self, *, who: str) -> bool:
        with self._lock:
            if self.done:
                return False
            self.done = True
        try:
            if self.work is not None:
                # Verify owned work is dead before releasing anything shared (never share a browser
                # with a still-running owned worker). terminate() is idempotent.
                # Normal completion keeps the owned daemon warm for the session's next call (its
                # lifetime is bound to the session via on_session_end); only CLI groups must be dead.
                # After a deadline kill (killed_all) EVERYTHING owned — daemon, env-marked detached
                # descendants, startup-race children — must be dead; otherwise fail CLOSED: the lease
                # is NOT released (a still-running owned browser must never be shared) and the
                # finalize fn is kept for `/guardrails recover`.
                strict = bool(self.work.killed_all)
                if self.work.alive(include_daemon=strict):
                    self.work.terminate(grace_s=2.0, stop_daemon=strict)
                survivors = self.work.alive(include_daemon=strict)
                if survivors:
                    self.work.cleanup_unconfirmed = True
                    self.work.deferred_finalize = self.fn
                    logger.error("guardrails: owned cleanup NOT confirmed (%s still alive: %s); lease retained (%s)",
                                 len(survivors), survivors[:5], who)
                    return True
                self.work.close()
            if self.fn is not None:
                self.fn()
            self.ran = True
            logger.debug("guardrails: finalized by %s", who)
        except Exception:
            logger.debug("guardrails: finalize failed (%s)", who, exc_info=True)
        return True


def run_with_deadline(next_call: Callable[[Any], Any], payload: Any, *, deadline_at: float,
                      kill_grace_s: float = 5.0, cleanup_reserve_s: float = DEFAULT_CLEANUP_RESERVE_S,
                      work: Optional[OW.OwnedWork] = None,
                      finalize: Optional[Callable[[], None]] = None) -> DeadlineOutcome:
    """Run ``next_call(payload)`` under one absolute deadline; see module docstring."""
    import contextvars
    t_start = time.monotonic()
    fin = _Finalizer(finalize, work)
    remaining = deadline_at - t_start
    if remaining <= 0:
        fin.run(who="caller-expired")
        return DeadlineOutcome(reason="deadline_exceeded", worker_finished=True, overrun_s=-remaining,
                              phases={"execution_s": 0.0, "cleanup_s": 0.0, "total_s": 0.0}, finalized=fin.ran)

    box: dict[str, Any] = {}
    done = threading.Event()
    returned_to_caller = threading.Event()
    ctx = contextvars.copy_context()  # downstream reads session/secret-scope contextvars

    def target():
        try:
            if work is not None:
                with OW.attach(work):
                    box["result"] = ctx.run(_run_attached, next_call, payload, work)
            else:
                box["result"] = ctx.run(next_call, payload)
        except BaseException as e:  # noqa: BLE001 - propagate via outcome
            box["error"] = e
        finally:
            box["t_end"] = time.monotonic()
            done.set()
            if returned_to_caller.is_set():
                fin.run(who="worker-late")   # late finalization: caller already gave up waiting

    th = threading.Thread(target=target, name="guardrail-deadline-worker", daemon=True)
    if work is not None:
        work.worker = th   # lets /guardrails recover tell "call still executing" from "worker gone"
    th.start()
    finished = done.wait(timeout=remaining)
    killed: list[int] = []
    report: dict = {}
    t_deadline = time.monotonic()
    if not finished and work is not None:
        report = work.terminate(grace_s=kill_grace_s)
        killed = list(report.get("pgids_killed") or [])
        finished = done.wait(timeout=max(0.0, cleanup_reserve_s))
    elif not finished:
        finished = done.wait(timeout=max(0.0, cleanup_reserve_s))
    # The CLI's own timeout can win the race with done.wait() at the deadline.
    # It kills its process group but may leave its detached browser daemon alive.
    # Reconcile all owned work BEFORE releasing the lease in either timeout path.
    from .breaker import classify
    inner_timeout = classify(box.get("result")) == "infra_timeout"
    if finished and work is not None and not report and (box.get("t_end", 0) > deadline_at or inner_timeout):
        report = work.terminate(grace_s=kill_grace_s)
        killed = list(report.get("pgids_killed") or [])
    t_cleanup = time.monotonic()
    if finished:
        fin.run(who="caller")
    else:
        returned_to_caller.set()
        if done.is_set():          # raced: worker finished just now
            fin.run(who="caller-race")
    t_end = box.get("t_end", t_cleanup)
    exec_s = t_end - t_start
    budget_s = deadline_at - t_start
    outcome = DeadlineOutcome(
        reason="ok", result=box.get("result"), error=box.get("error"), killed_pids=killed,
        worker_finished=finished, overrun_s=max(0.0, exec_s - budget_s),
        phases={"execution_s": round(exec_s, 3), "cleanup_s": round(max(0.0, t_cleanup - t_deadline), 3),
                "total_s": round(time.monotonic() - t_start, 3)},
        owned_report=report, finalized=fin.ran,
    )
    if "error" in box:
        outcome.reason = "worker_error"
    if exec_s > budget_s or killed or not finished or inner_timeout:
        outcome.reason = "deadline_exceeded"
    return outcome


def _run_attached(next_call, payload, work):
    with OW.attach(work):
        return next_call(payload)
