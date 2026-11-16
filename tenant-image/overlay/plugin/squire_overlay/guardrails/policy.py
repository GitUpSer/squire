"""Guardrail policy: wires the building blocks into Hermes via PUBLIC plugin seams.

Seams (all documented plugin API, pinned by overlay/tests/contract):

* ``ctx.register_middleware("tool_execution", ...)`` — wraps every registry tool dispatch in
  every agent context on this host (main loop, concurrent executor, execute_code RPC bridge,
  in-process subagents). This is where lease / breaker / preflight / deadline enforcement lives.
* hooks ``subagent_start`` / ``subagent_stop`` / ``on_session_end`` / ``agent_loop_stopped`` —
  feed the harness-owned ledger so a parent's origin ledger sees child progress even when the
  model never writes a report.
* ``ctx.register_command("guardrails")`` and ``ctx.register_cli_command("guardrails")`` —
  operator-facing effective-policy / status surface.

Interactive-browser tools = every tool in the ``browser`` toolset (``browser_exec``,
``browser_cdp``, ``browser_navigate``, …) plus the ``browser_vault_*`` tools. Lightweight
search/HTTP tools are only ledgered (never leased).

Everything here fails OPEN for non-browser tools and fails CLOSED-with-a-structured-reason for
browser tools: the model always gets a JSON object naming the reason code and what to do.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import secrets
import threading
import time
from typing import Any, Callable, Optional

from . import breaker as BR
from . import deadline as DL
from . import findings as FD
from . import ledger as LG
from . import lease as LS
from . import owned as OW
from . import preflight
from . import progress as PG

logger = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    "enforcement": True,
    "browser_capacity": 1,            # host-wide interactive browsers
    "lease_wait_s": 90.0,             # how long a caller may queue for the browser
    "lease_max_queue": 8,
    "startup_allowance_s": 45.0,      # added to the tool's own timeout_s for the end-to-end budget
    "kill_grace_s": 10.0,
    "cleanup_reserve_s": 10.0,        # bounded wait for the worker thread after the kill, then late finalization
    "consecutive_limit": BR.CONSECUTIVE_LIMIT,
    "recovery_budget_s": BR.DEFAULT_RECOVERY_BUDGET_S,
    "breaker_reset_after_s": BR.DEFAULT_RESET_AFTER_S,   # per-task breaker state forgotten this long after the last infra failure
    "notify_first_after_s": PG.DEFAULT_FIRST_AFTER_S,
    "notify_every_s": PG.DEFAULT_EVERY_S,
    "notify_max_age_s": PG.DEFAULT_MAX_AGE_S,            # in-flight entries older than this mark the ledger stale: notices stop
    "notify": True,
    "notify_groups": False,           # group chats NEVER get automatic notices unless explicitly opted in
    "notify_chats": [],               # allow-list (exact chat id or bare part before "@"); empty = every non-group chat
    "max_chromium": preflight.DEFAULT_MAX_CHROMIUM,
    "block_on_findings": True,
    "resource_scope": False,          # container is the outer resource boundary; no systemd user bus
    "scope_slice": "hermes-browser.slice",
    "scope_memory_max": "1200M",
    "scope_memory_high": "900M",
    "scope_cpu_weight": 50,
}
# Automatic browser-call dedupe is intentionally ABSENT (deferred): the only safe form needs explicit
# read-only semantics from the tool itself (a navigation is not read-only — logout/approve/signed/
# one-time URLs, per-session cookies). Every browser call runs exactly once; no result is ever
# replayed to another caller. Only metadata findings (auth_required / site_unreachable) are shared.
BROWSER_DEPENDENCY = "browser"
_URL_RX = re.compile(r"https?://[^\s'\"()<>]+", re.I)
# Escape hatch for `finding_known`: browser_exec's schema has no extra argument the model could set
# (browser_use_cli ignores unknown args), so the override travels INSIDE `code` as a comment line.
# The middleware strips nothing; a comment is harmless to the CLI.
_IGNORE_FINDINGS_RX = re.compile(r"^\s*#\s*guardrails?\s*:\s*ignore[-_ ]findings\b", re.I | re.M)
_IGNORE_FINDINGS_HINT = "# guardrails: ignore-findings"
_TIMEOUT_MIN, _TIMEOUT_MAX, _TIMEOUT_DEFAULT = 5, 1800, 300

_ctx = None
_lock = threading.Lock()
_session_origins: dict[str, dict] = {}      # child/any session_id -> origin (populated by hooks)
_child_sessions: set[str] = set()
_notifier: Optional[PG.Notifier] = None
_recent_reasons: list[dict] = []


# ---------------------------------------------------------------------------------------------
# settings / identity
# ---------------------------------------------------------------------------------------------

def _settings() -> dict:
    cfg = dict(DEFAULTS)
    if _ctx is not None:
        try:
            user = _ctx.get_config("guardrails", {}) or {}
            if isinstance(user, dict):
                cfg.update({k: v for k, v in user.items() if k in DEFAULTS})
        except Exception:
            pass
    return cfg


def _notice_scope(cfg: dict) -> PG.NoticeScope:
    """Build the progress-notice scope from effective settings. ``notify_chats`` may arrive as a
    list, a single scalar, or a comma-separated string (hand-edited YAML); all are tolerated."""
    raw = cfg.get("notify_chats")
    if raw is None or isinstance(raw, (bool, dict)):
        # YAML `notify_chats: yes` would otherwise become the allow-list ["True"] and silence
        # every chat including the owner. A bool/dict is a mistake: ignore it, say so once.
        if raw not in (None, False):
            logger.warning("guardrails.notify_chats must be a list of chat ids, got %r; ignoring it", raw)
        chats: list = []
    elif isinstance(raw, (list, tuple, set)):
        chats = list(raw)
    else:
        chats = str(raw).split(",")
    return PG.NoticeScope(notify_chats=chats, notify_groups=bool(cfg.get("notify_groups", False)))


def _session_env(name: str) -> str:
    try:
        from gateway.session_context import get_session_env
        return get_session_env(name) or ""
    except Exception:
        return os.getenv(name, "")


def _current_origin(session_id: str = "") -> dict:
    """Origin (chat) of the CURRENT turn: gateway contextvars first (children inherit the parent's
    copied context), then the hook-populated parent map (subagent_start), else a UNIQUE, untrusted
    per-invocation origin. Untrusted origins never collapse into a shared ``session:anon`` scope, so
    two unrelated local/RPC callers cannot see each other's findings or ledger (fail closed)."""
    platform = _session_env("HERMES_SESSION_PLATFORM")
    chat_id = _session_env("HERMES_SESSION_CHAT_ID")
    key = _session_env("HERMES_SESSION_KEY")
    if chat_id or key:
        return {"platform": platform or "unknown", "chat_id": chat_id, "session_key": key or f"{platform}:{chat_id}",
                "trusted": True}
    with _lock:
        known = _session_origins.get(session_id or "")
    if known:
        return dict(known)
    if session_id:
        # A real session id without gateway context (CLI / RPC bridge): scoped to THAT session only.
        return {"platform": platform or "local", "chat_id": "", "session_key": f"session:{session_id}", "trusted": False}
    return {"platform": platform or "local", "chat_id": "", "session_key": f"anon:{os.getpid()}:{secrets.token_hex(6)}",
            "trusted": False}


BROWSER_TOOLSETS = frozenset({"browser", "browser-use"})


def _is_browser_tool(tool_name: str) -> bool:
    if tool_name.startswith("browser_"):
        return True
    try:
        from tools.registry import registry
        return registry.get_toolset_for_tool(tool_name) in BROWSER_TOOLSETS
    except Exception:
        return False


def _note_reason(code: str, tool: str, detail: str = "") -> None:
    with _lock:
        _recent_reasons.append({"at": time.time(), "code": code, "tool": tool, "detail": detail[:120]})
        del _recent_reasons[:-50]


def reset_for_tests() -> None:
    with _lock:
        _session_origins.clear()
        _child_sessions.clear()
        _recent_reasons.clear()


# ---------------------------------------------------------------------------------------------
# result helpers
# ---------------------------------------------------------------------------------------------

def _tool_error(message: str, **extra) -> str:
    try:
        from tools.registry import tool_error
        return tool_error(message, **extra)
    except Exception:
        return json.dumps({"error": message, **extra})


def _attach(result: Any, info: dict) -> Any:
    """Add a ``guardrail`` block to a JSON-object tool result; leave other shapes untouched."""
    if isinstance(result, dict):
        result = dict(result); result["guardrail"] = info
        return result
    if isinstance(result, str) and result.lstrip().startswith("{"):
        try:
            obj = json.loads(result)
        except Exception:
            return result
        if isinstance(obj, dict):
            obj["guardrail"] = info
            return json.dumps(obj, ensure_ascii=False)
    return result


def _refusal(led: LG.Ledger, origin: dict, *, task_id: str, tool_call_id: str, t0: float, code: str,
             message: str, guardrail: dict) -> str:
    """Every guardrail refusal returns through here so the ledger entry opened by ``tool_started`` is
    CLOSED (status ``refused``) — otherwise it would sit in ``in_flight`` until the turn ends, the
    progress notifier would report a phantom running tool and the counters would skew. The task's
    blocker is (re)set AFTER closing the entry so ``/guardrails`` and the next progress notice still
    name the reason; the task's next finished tool call or the task end clears it."""
    led.tool_finished(origin, tool_call_id=tool_call_id, status="refused", reason=code,
                      duration_s=round(time.monotonic() - t0, 1))
    led.set_blocker(origin, task_id=task_id, code=code, detail=str(message)[:160])
    return _tool_error(message, guardrail=guardrail)


def _clamp_timeout(v: Any) -> int:
    try:
        return max(_TIMEOUT_MIN, min(int(v), _TIMEOUT_MAX))
    except (TypeError, ValueError):
        return _TIMEOUT_DEFAULT


def _hosts_in(args: dict) -> list[str]:
    text = " ".join(str(args.get(k) or "") for k in ("code", "url", "urls"))
    return sorted({FD.host_of(u) for u in _URL_RX.findall(text)})


def _scope_for(cfg: dict) -> Optional[dict]:
    if not cfg.get("resource_scope", True):
        return None
    return {"slice": cfg.get("scope_slice") or "", "memory_max": cfg.get("scope_memory_max"),
            "memory_high": cfg.get("scope_memory_high"), "cpu_weight": cfg.get("scope_cpu_weight")}


# ---------------------------------------------------------------------------------------------
# the middleware
# ---------------------------------------------------------------------------------------------

def _guarded_browser_call(tool_name: str, args: dict, next_call, *, origin: dict, task_id: str,
                          session_id: str, tool_call_id: str, cfg: dict, led: LG.Ledger) -> Any:
    t0 = time.monotonic()
    # ONE absolute deadline for everything that follows (queue, preflight, startup, work, cleanup).
    timeout_s = _clamp_timeout(args.get("timeout_s"))
    deadline_at = t0 + timeout_s + float(cfg["startup_allowance_s"])
    phases: dict[str, float] = {}
    origin_key = LG.task_key(origin)
    trusted = bool(origin.get("trusted", False))
    owner = session_id or task_id or "anon"
    base_info = {"origin_scope": origin_key, "deadline_s": round(deadline_at - t0, 1)}

    # 1. syntax precheck (before any startup cost)
    if tool_name == "browser_exec":
        err = DL.precheck_code(str(args.get("code") or ""))
        if err:
            _note_reason("code_syntax_error", tool_name, err["message"])
            return _refusal(led, origin, task_id=task_id, tool_call_id=tool_call_id, t0=t0, code="code_syntax_error",
                            message=f"browser code rejected before launch: {err['message']}",
                            guardrail={"reason": "code_syntax_error", **err, **base_info})

    # 2. shared findings for the hosts this call targets (scope = origin; untrusted origins are unique)
    fnd = FD.Findings(scope=origin_key)
    hosts = _hosts_in(args)
    known = [f for h in hosts for f in fnd.lookup(h)]
    # `_ignore_findings` is kept for callers that can pass extra args (tests, RPC); browser_exec's
    # schema cannot, so the model's real override is the comment line in `code` (see _IGNORE_FINDINGS_RX).
    ignore = bool(args.pop("_ignore_findings", False)) or bool(_IGNORE_FINDINGS_RX.search(str(args.get("code") or "")))
    blocking = [f for f in known if f["kind"] in ("auth_required", "site_unreachable")]
    if blocking and cfg["block_on_findings"] and not ignore:
        f = blocking[0]
        _note_reason("finding_known", tool_name, f"{f['host']} {f['kind']}")
        expires_in = max(0, int(FD.DEFAULT_TTL_S - (time.time() - float(f.get("at") or time.time()))))
        return _refusal(
            led, origin, task_id=task_id, tool_call_id=tool_call_id, t0=t0, code="finding_known",
            message=f"{f['host']} already reported {f['kind']} by sibling task {f['task_id']} ({f['detail']}; "
                    f"expires in {expires_in}s). Do not retry blindly: ask the user (auth) or skip the site (unreachable). "
                    f"If you have new information (e.g. the user has logged in), put the line `{_IGNORE_FINDINGS_HINT}` "
                    "at the top of your code to run this call anyway.",
            guardrail={"reason": "finding_known", "findings": known, "override": _IGNORE_FINDINGS_HINT, **base_info})

    # (No in-flight dedupe: see the note under DEFAULTS. Each call runs itself, under the lease.)
    del trusted  # findings scope already encodes trust (untrusted origins are unique)
    return _guarded_browser_call_leased(tool_name, args, next_call, origin=origin, task_id=task_id, owner=owner,
                                        tool_call_id=tool_call_id, cfg=cfg, led=led, t0=t0, deadline_at=deadline_at,
                                        phases=phases, fnd=fnd, hosts=hosts, known=known, base_info=base_info,
                                        origin_key=origin_key)


def _guarded_browser_call_leased(tool_name: str, args: dict, next_call, *, origin: dict, task_id: str, owner: str,
                                 tool_call_id: str, cfg: dict, led: LG.Ledger, t0: float, deadline_at: float,
                                 phases: dict, fnd: FD.Findings, hosts: list, known: list, base_info: dict,
                                 origin_key: str) -> Any:

    # 3. breaker
    br = BR.Breaker(BROWSER_DEPENDENCY, task_id=origin_key, recovery_budget_s=cfg["recovery_budget_s"],
                    consecutive_limit=cfg["consecutive_limit"], reset_after_s=float(cfg["breaker_reset_after_s"]))
    v = br.check()
    if not v.allowed:
        _note_reason(v.reason, tool_name)
        heal = (f" This task's breaker state resets {v.resets_in_s:.0f}s after its last infra failure, "
                f"on the next successful browser call, or via /guardrails recover." if v.resets_in_s else "")
        return _refusal(
            led, origin, task_id=task_id, tool_call_id=tool_call_id, t0=t0, code=v.reason,
            message=f"browser infrastructure circuit is open ({v.reason}; {v.failures} consecutive infra failures, "
                    f"{v.spent_s:.0f}s recovery spent). Do NOT retry the browser in this task; report the blocker to the user "
                    f"or use web_extract/web_search instead." + (f" Host retry possible in {v.retry_after_s:.0f}s." if v.retry_after_s else "") + heal,
            guardrail={"reason": v.reason, **v.as_dict(), "findings": known, **base_info})

    # 4. host preflight
    adm = preflight.admit(preflight.read_host(), max_chromium=int(cfg["max_chromium"]))
    phases["preflight_s"] = round(time.monotonic() - t0, 3)
    if not adm.allowed:
        _note_reason(adm.reason, tool_name, adm.message)
        br.release_probe()   # a half-open probe slot claimed by check() must not stay occupied by a refused call
        return _refusal(led, origin, task_id=task_id, tool_call_id=tool_call_id, t0=t0, code=adm.reason,
                        message=f"browser launch refused: {adm.message}",
                        guardrail={"reason": adm.reason, "metrics": adm.metrics, "findings": known, **base_info})

    # 5. host-wide lease — the queue wait is bounded by BOTH lease_wait_s and the absolute deadline,
    #    minus a reserved cleanup grace so a call admitted at the last moment still has time to run.
    t_q = time.monotonic()
    reserve = float(cfg["kill_grace_s"]) + float(cfg["cleanup_reserve_s"])
    wait_s = max(0.0, min(float(cfg["lease_wait_s"]), deadline_at - t_q - reserve))
    led.set_blocker(origin, task_id=task_id, code="lease_wait", detail="waiting for the host-wide browser lease")
    lease = LS.acquire(LS.lease_name_for_browser(session=str(args.get("session") or "")), owner=owner,
                       wait_s=wait_s, capacity=int(cfg["browser_capacity"]),
                       max_queue=int(cfg["lease_max_queue"]), meta={"tool": tool_name, "task": origin_key})
    phases["queue_s"] = round(time.monotonic() - t_q, 3)
    led.clear_blocker(origin, task_id=task_id)
    if not lease.acquired:
        _note_reason(lease.reason, tool_name, f"holder={lease.holder}")
        br.release_probe()   # same: the probe slot goes back when the call is not admitted
        return _refusal(
            led, origin, task_id=task_id, tool_call_id=tool_call_id, t0=t0, code=lease.reason,
            message=f"browser lease unavailable after {lease.waited_s:.0f}s ({lease.reason}; this call's deadline is "
                    f"{deadline_at - t0:.0f}s end-to-end); held by {lease.holder}. "
                    "Only one interactive browser runs on this host. Do not spin: wait for the holder to finish, "
                    "or use web_extract for read-only pages.",
            guardrail={"reason": lease.reason, "holder": lease.holder, "waited_s": round(lease.waited_s, 1),
                       "findings": known, **base_info})
    if lease.recovered_from_crash:
        _note_reason("lease_recovered_from_crash", tool_name)

    # 6. execution under the SAME absolute deadline, with call-owned process tracking
    budget = deadline_at - t0
    t_exec = time.monotonic()
    lease_ref = {"lease": lease}

    def _finalize():
        # Runs exactly once, only after the worker thread returned AND owned work is verified dead
        # (deadline._Finalizer) — by the caller, or late by the worker thread itself.
        lease_ref["lease"].release()
        LS.note_finalized(owner)

    work: Optional[OW.OwnedWork] = None
    try:
        # Owned daemon runtime dir is per (THIS process, origin, session[, BU_NAME]): warm across calls
        # of one session, stopped with verified ownership at on_session_end, never shared across origins
        # or across Hermes processes (a restart gets a new dir; the old daemon is an orphan, never
        # adopted). The harness runs ONE daemon per runtime dir (bare `bu.*` stems under BH_RUNTIME_DIR),
        # so a named `session` (BU_NAME) gets its own dir — otherwise two names would fight over one
        # daemon — inside the same session group, which is what on_session_end stops.
        session_group = f"{origin_key}:{owner}"
        bu_name = str(args.get("session") or "")
        work = OW.OwnedWork(key=f"{origin_key}:{owner}:{tool_call_id}",
                            runtime_dir=OW.runtime_dir_for(session_group + (f":{bu_name}" if bu_name else ""),
                                                           group=session_group),
                            scope=_scope_for(cfg))
        out = DL.run_with_deadline(
            next_call, args, deadline_at=deadline_at, kill_grace_s=float(cfg["kill_grace_s"]),
            cleanup_reserve_s=float(cfg["cleanup_reserve_s"]), work=work, finalize=_finalize,
        )
    except BaseException:
        # Between acquiring the host-wide flock and the finalizer taking ownership of it, anything
        # can raise (runtime-dir mkdir OSError, Thread.start() RuntimeError under thread exhaustion…).
        # The outer middleware swallows that exception and nothing else references this Lease object,
        # so without this block the flock would be held by this process until it exits. Lease.release
        # is idempotent, so a finalizer that does run later is harmless.
        _note_reason("lease_released_on_wrapper_error", tool_name)
        with contextlib.suppress(Exception):
            _finalize()
        with contextlib.suppress(Exception):
            br.release_probe()
        if work is not None:
            with contextlib.suppress(Exception):
                work.close()
        raise
    phases.update({k: v for k, v in out.phases.items() if k != "total_s"})
    if not out.worker_finished:
        # The worker thread is still inside upstream (e.g. a pipe drain). Owned processes were killed;
        # the lease is released by LATE finalization the moment the worker returns — never before.
        _note_reason("worker_late_finalization_pending", tool_name)
        LS.note_pending(owner, tool_call_id=tool_call_id, work_id=work.id)
        logger.error("guardrails: browser worker for %s did not return within reserve; late finalization armed", tool_call_id)
    if work.cleanup_unconfirmed:
        # Fail closed: an owned process survived every kill step. The lease stays held so no other
        # caller shares the host with it; `/guardrails recover` re-verifies and releases.
        _note_reason("owned_cleanup_unconfirmed", tool_name, work.id)
        LS.note_pending(owner, tool_call_id=tool_call_id, work_id=work.id)
        logger.error("guardrails: owned cleanup for %s unconfirmed; lease retained", tool_call_id)

    # 7. classify + record
    t_fin = time.monotonic()
    result = out.result
    infra: Optional[str] = None
    if out.reason == "deadline_exceeded":
        infra = "infra_timeout"
    elif out.reason == "worker_error":
        infra = "infra_launch_failed" if isinstance(out.error, OSError) else None
        result = _tool_error(f"browser tool raised {type(out.error).__name__}: {str(out.error)[:200]}")
    else:
        infra = BR.classify(result)
    cost = time.monotonic() - t_exec
    policy = BR.retry_policy(tool_name, args, infra)
    if infra:
        br.record_failure(infra, cost_s=cost, detail=f"{tool_name} {out.reason}")
        _note_reason(infra, tool_name, out.reason)
    else:
        br.record_success()
        for h in hosts:
            kind = FD.classify_result(h, result if isinstance(result, str) else json.dumps(result, default=str))
            if kind:
                fnd.record(host=h, kind=kind, detail=f"seen by {tool_name}", task_id=task_id)
    info: dict[str, Any] = {
        "reason": "ok" if out.reason == "ok" else out.reason,
        "budget_s": round(budget, 1), "overrun_s": round(out.overrun_s, 1),
        "killed_processes": len(out.killed_pids), "worker_finished": out.worker_finished,
        "retry_policy": policy, "lease_owner": owner, "owned": {
            "pgids": list(work.pgids), "daemon": (out.owned_report.get("daemon") or {}).get("stopped"),
            "marked_killed": len(out.owned_report.get("marked_killed") or []),
            "cleanup_confirmed": not work.cleanup_unconfirmed,
            "scope": work.scope_unit if work.scope_applied else (work.scope_error or None)},
        **base_info,
    }
    if infra:
        info["infra_reason"] = infra
        bv = br.check()
        info["breaker"] = bv.as_dict()
    if known:
        info["findings"] = known
    if out.reason == "deadline_exceeded":
        base = (f"browser call exceeded its end-to-end deadline of {budget:.0f}s (configured timeout_s="
                f"{_clamp_timeout(args.get('timeout_s'))} + startup allowance, measured from dispatch incl. queueing). "
                f"Killed {len(out.killed_pids)} owned worker process group(s). retry_policy={policy}. "
                "Anything written to the workspace is preserved.")
        result = _tool_error(base, guardrail=info)
    else:
        result = _attach(result, info)
    phases["finalization_s"] = round(time.monotonic() - t_fin, 3)
    phases["total_s"] = round(time.monotonic() - t0, 3)
    led.tool_finished(origin, tool_call_id=tool_call_id, status="ok" if info["reason"] == "ok" and not infra else "error",
                      reason=infra or info["reason"], duration_s=round(time.monotonic() - t0, 1), telemetry=phases)
    return result


def _tool_execution_middleware(*, tool_name: str = "", args: Any = None, next_call=None, task_id: str = "",
                               session_id: str = "", tool_call_id: str = "", **_: Any) -> Any:
    args = args if isinstance(args, dict) else {}
    cfg = _settings()
    if not cfg.get("enforcement", True) or next_call is None:
        return next_call(args) if next_call else None
    led = LG.Ledger()
    origin = _current_origin(session_id)
    call_id = tool_call_id or f"{tool_name}-{int(time.time() * 1000)}"
    led.tool_started(origin, task_id=task_id or session_id or "root", tool_call_id=call_id, tool_name=tool_name,
                     session_id=session_id)
    if not _is_browser_tool(tool_name):
        t0 = time.monotonic()
        try:
            res = next_call(args)
            led.tool_finished(origin, tool_call_id=call_id, status="ok", duration_s=round(time.monotonic() - t0, 1))
            return res
        except BaseException as e:
            led.tool_finished(origin, tool_call_id=call_id, status="error", reason=type(e).__name__,
                              duration_s=round(time.monotonic() - t0, 1))
            raise
    try:
        return _guarded_browser_call(tool_name, args, next_call, origin=origin, task_id=task_id or "root",
                                     session_id=session_id, tool_call_id=call_id, cfg=cfg, led=led)
    except BaseException as e:  # never let the guardrail itself wedge a turn
        logger.exception("guardrails: wrapper failed for %s", tool_name)
        led.tool_finished(origin, tool_call_id=call_id, status="error", reason=f"guardrail_error:{type(e).__name__}")
        return _tool_error(f"guardrail wrapper error: {type(e).__name__}: {str(e)[:200]}",
                           guardrail={"reason": "guardrail_error"})


# ---------------------------------------------------------------------------------------------
# lifecycle hooks → ledger
# ---------------------------------------------------------------------------------------------

def _on_subagent_start(**kw) -> None:
    parent = str(kw.get("parent_session_id") or "")
    child = str(kw.get("child_session_id") or "")
    origin = _current_origin(parent)
    with _lock:
        if child:
            _session_origins[child] = origin
            _child_sessions.add(child)
    LG.Ledger().child_started(origin, parent_task_id=parent, child_task_id=child or "child",
                              goal=str(kw.get("child_goal") or "")[:120])


_TURN_MARK = "llm:turn"


def _on_pre_llm_call(**kw) -> None:
    """Ledger entry from the moment a turn is DISPATCHED (before any tool, while the model may hang):
    the progress notifier therefore sees the turn as in-flight from t0, not from the first tool call."""
    sid = str(kw.get("session_id") or "")
    origin = _current_origin(sid)
    if not origin.get("chat_id"):
        return  # non-messaging origin: nothing to notify, nothing to track
    LG.Ledger().turn_started(origin, task_id=str(kw.get("task_id") or sid or "root"),
                             turn_id=str(kw.get("turn_id") or ""), session_id=sid, marker=_TURN_MARK)
    return None


def _on_post_llm_call(**kw) -> None:
    sid = str(kw.get("session_id") or "")
    origin = _current_origin(sid)
    if not origin.get("chat_id"):
        return
    LG.Ledger().turn_finished(origin, turn_id=str(kw.get("turn_id") or ""), marker=_TURN_MARK)


def _on_subagent_stop(**kw) -> None:
    child = str(kw.get("child_session_id") or "")
    origin = _current_origin(kw.get("parent_session_id") or "")
    status = str(kw.get("child_status") or "unknown")
    LG.Ledger().child_finished(origin, child_task_id=child or "child", status=status,
                               summary=str(kw.get("child_summary") or "")[:200])


def _on_session_end(**kw) -> None:
    sid = str(kw.get("session_id") or "")
    with _lock:
        is_child = sid in _child_sessions
    if is_child:
        return  # the parent's subagent_stop finalizes children
    origin = _current_origin(sid)
    if kw.get("interrupted"):
        status = "interrupted"
    elif kw.get("failed"):
        status = "failed"
    else:
        status = "completed"
    LG.Ledger().task_finished(origin, task_id=str(kw.get("task_id") or "root"), status=status)
    _stop_session_daemon(LG.task_key(origin), sid)


def _stop_session_daemon(origin_key: str, session_id: str) -> Optional[dict]:
    """Stop the harness daemon(s) owned by (this process, origin, session) — ownership verified via
    /proc environ against the in-memory marker; anything else is never signalled. Env-marked detached
    descendants (startup race, Chromium) are killed too; the marker is then forgotten.

    Fail safe: a dir still used by live owned work of this process (a call whose worker is inside
    upstream / late-finalizing when the session ends) is NOT touched now; the stop is deferred to the
    moment that work closes (``OW.mark_session_ended``). Returns one report for the single-dir case,
    else ``{"dirs": [...]}`` with the union fields."""
    if not session_id:
        return None
    dirs = OW.known_runtime_dirs(f"{origin_key}:{session_id}")
    if not dirs:
        return None  # this process never started a daemon for that session: nothing to own
    reports = []
    for rt in dirs:
        busy = OW.shared_with_live_work(rt)
        if busy:
            OW.mark_session_ended(rt)
            _note_reason("owned_daemon_stop_deferred", "session_end", f"{len(busy)} live owned work in {rt}")
            reports.append({"runtime_dir": rt, "pid": None, "stopped": False, "reason": "deferred_in_use",
                            "marked_killed": [], "cleanup_confirmed": False, "others": [w.id for w in busy]})
            continue
        rep = OW.stop_owned_daemon(rt, grace_s=3.0)
        rep["marked_killed"] = OW.kill_marked(rt, grace_s=3.0)
        survivors = OW.marked_processes(rt)
        rep["cleanup_confirmed"] = not survivors
        if rep.get("pid") and rep.get("reason") not in ("no_pid_file", "already_dead"):
            _note_reason("owned_daemon_stopped" if rep.get("stopped") else "owned_daemon_not_stopped", "session_end", rep.get("reason", ""))
        if survivors:
            _note_reason("owned_cleanup_unconfirmed", "session_end", f"{len(survivors)} alive in {rt}")
            logger.error("guardrails: session-end cleanup unconfirmed for %s: %s still alive", rt, survivors[:5])
        else:
            OW.forget_runtime_dir(rt)
        reports.append(rep)
    if len(reports) == 1:
        return reports[0]
    return {"dirs": reports, "reason": "multiple", "stopped": all(r.get("stopped") for r in reports),
            "marked_killed": [p for r in reports for p in r.get("marked_killed", [])],
            "cleanup_confirmed": all(r.get("cleanup_confirmed") for r in reports)}


def _on_agent_loop_stopped(**kw) -> None:
    key = str(kw.get("session_key") or "")
    if key:
        LG.Ledger().task_finished({"platform": kw.get("platform", ""), "chat_id": "", "session_key": key},
                                  task_id="root", status="interrupted")


# ---------------------------------------------------------------------------------------------
# status surface
# ---------------------------------------------------------------------------------------------

def status_text() -> str:
    cfg = _settings()
    lines = [
        "Latency guardrails — effective policy",
        f"  enforcement: {'ON' if cfg['enforcement'] else 'OFF'}  (managed overlay default; not memory/skill dependent)",
        f"  browser_capacity={cfg['browser_capacity']} lease_wait_s={cfg['lease_wait_s']:.0f} lease_max_queue={cfg['lease_max_queue']}",
        f"  deadline = ONE absolute instant: dispatch t0 + timeout_s(5..1800) + startup_allowance_s={cfg['startup_allowance_s']:.0f} "
        f"(queue+preflight+startup+work+cleanup); kill_grace_s={cfg['kill_grace_s']:.0f} cleanup_reserve_s={cfg['cleanup_reserve_s']:.0f}",
        f"  ownership: spawn seam {'INSTALLED' if OW.seam_installed() else 'NOT installed (browser_use_cli unavailable)'}; "
        f"only call-owned pgids/daemon/scope are ever killed",
        f"  resource scope: {'on' if cfg['resource_scope'] else 'off'} slice={cfg['scope_slice']} MemoryMax={cfg['scope_memory_max']} "
        f"MemoryHigh={cfg['scope_memory_high']} CPUWeight={cfg['scope_cpu_weight']} (systemd --user; off automatically where unavailable)",
        f"  breaker: consecutive_limit={cfg['consecutive_limit']} recovery_budget_s={cfg['recovery_budget_s']:.0f} per origin task "
        f"(state resets on success, {float(cfg['breaker_reset_after_s']):.0f}s after the last failure, or /guardrails recover); host backoff 60s×2^n",
        f"  preflight: load1 > {preflight.LOAD_PER_CPU_LIMIT:.0f}×cores | avail < {preflight.MIN_AVAIL_MB}MB | swap > {preflight.MAX_SWAP_USED_PCT}% | chromium > {cfg['max_chromium']} → refuse",
        f"  progress notices: first after {cfg['notify_first_after_s']:.0f}s, then every {cfg['notify_every_s']:.0f}s, per origin chat "
        f"({'on' if cfg['notify'] else 'off'}); non-DM chats {'INCLUDED' if cfg.get('notify_groups') else 'excluded'}; "
        f"chats {list(_notice_scope(cfg).configured) or 'any DM'}; "
        f"ledgers with entries older than {float(cfg['notify_max_age_s']):.0f}s or from dead processes are retired",
        "  probe dedupe: NONE (deferred) — every browser call runs itself; only auth_required/site_unreachable findings are shared",
        "",
        "Reason codes: lease_wait_timeout lease_queue_full lease_recovered_from_crash deadline_exceeded code_syntax_error",
        "  infra_timeout infra_launch_failed infra_cdp_unreachable infra_cli_missing infra_resource_pressure",
        "  breaker_open_consecutive_failures breaker_open_recovery_budget dependency_unhealthy_host dependency_half_open_probe_in_flight",
        "  host_pressure_load host_pressure_memory host_pressure_swap host_pressure_chromium_count finding_known",
        "  worker_late_finalization_pending owned_cleanup_unconfirmed lease_released_on_wrapper_error owned_daemon_stop_deferred",
        "",
    ]
    hs = LS.holders(BROWSER_DEPENDENCY, capacity=int(cfg["browser_capacity"]))
    lines.append("Browser lease: " + (", ".join(f"{h.get('owner')} pid={h.get('pid')} ({int(time.time() - float(h.get('started', time.time())))}s)" for h in hs) if hs else "free")
                 + f"; waiters queued: {LS.queue_depth(BROWSER_DEPENDENCY)}")
    pend = LS.pending_finalizations()
    if pend:
        lines.append("Late finalizations pending: " + "; ".join(
            f"{p['owner']} call={p['tool_call_id']} pid={p['pid']} alive={p['process_alive']} ({int(time.time() - float(p['since']))}s)" for p in pend))
    live = OW.live_work()
    if live:
        lines.append(f"Owned work in this process: {len(live)} (" + ", ".join(f"{w.id}:{len(w.alive())} alive" for w in live[:5]) + ")")
    with contextlib.suppress(Exception):
        r = preflight.read_host()
        adm = preflight.admit(r, max_chromium=int(cfg["max_chromium"]))
        lines.append(f"Host now: load1={r.load1:.2f}/{r.ncpu}cpu avail={r.avail_mb}MB swap={r.swap_used_pct:.0f}% "
                     f"chromium={r.chromium_procs} harness_daemons={r.harness_daemons} (stale>{preflight.STALE_DAEMON_S // 3600}h: "
                     f"{r.stale_harness_daemons}) → admission={'ok' if adm.allowed else adm.reason}")
    for dep in BR.list_dependencies():
        snap = BR.snapshot(dep)
        host = snap.get("host", {})
        lines.append(f"Breaker[{dep}]: host={host.get('state')} trips={host.get('trips', 0)} backoff={host.get('backoff_s', 0):.0f}s")
        for tid, t in list(snap.get("tasks", {}).items())[-5:]:
            last = (t.get("history") or [{}])[-1]
            age = int(time.time() - float(t.get("last_failure_at") or 0)) if t.get("last_failure_at") else None
            lines.append(f"  task {tid}: consecutive={t.get('consecutive')} spent={t.get('spent_s', 0):.0f}s last={last.get('reason', '-')}"
                         + (f" ({age}s ago; forgotten after {float(cfg['breaker_reset_after_s']):.0f}s)" if age is not None else ""))
    active = LG.Ledger().list_active()
    lines.append(f"Active origin tasks: {len(active)}")
    for d in active[:5]:
        o = d.get("origin", {})
        lines.append(f"  {o.get('platform')}:{o.get('chat_id') or '-'} in_flight={len(d.get('in_flight', {}))} "
                     f"children={len(d.get('children', {}))} errors={d.get('counters', {}).get('tool_errors', 0)} "
                     f"blockers={','.join(b.get('code', '') for b in d.get('blockers', {}).values()) or '-'}")
    with _lock:
        recent = list(_recent_reasons[-8:])
    if recent:
        lines.append("Recent reason codes: " + "; ".join(f"{r['code']}({r['tool']})" for r in recent))
    lines.append(f"State dir: {LS.state_dir()}")
    return "\n".join(lines)


def _cmd_guardrails(raw_args: str) -> str:
    """Chat command. Home-chat only (approvals._authorized): the status lists other chats' session
    keys / ids and `recover` kills owned processes and clears host-wide state, so any reachable chat
    must not be able to invoke it. The CLI `hermes guardrails` is the operator's local surface."""
    from ..config import RuntimeConfig
    scope = RuntimeConfig.from_env()
    origin = _current_origin()
    if scope.kind != "dm" or origin.get("platform") != "squire" or origin.get("chat_id") != scope.conversation_id:
        return "This command is available only in the owner's private conversation."
    arg = (raw_args or "").strip().lower()
    if arg.startswith("recover"):
        return recover_text()
    return status_text()


def recover_text() -> str:
    """Operator recovery: verifies ownership and liveness before touching anything; never blanket-kills.
    Owned work in THIS process is touched ONLY when its deadline worker thread is gone (the call is
    over as far as upstream is concerned): a call still executing — or late-finalizing — is reported
    and left alone, because it releases itself. Stale pending records from dead processes are
    reported (their flocks are already released by the kernel) and cleared; per-task breaker state
    and stale lease-queue tickets are cleared too."""
    lines = ["Guardrail recovery (ownership-verified, no blanket kills):"]
    acted = 0
    for w in OW.live_work():
        if w.worker is not None and w.worker.is_alive():
            # the browser call is still running inside upstream (possibly late finalization armed):
            # killing its processes now would be exactly the blanket kill this command must not do
            lines.append(f"  owned work {w.id}: call still executing (worker thread alive); not touched")
            continue
        if w.cleanup_unconfirmed:
            # fail-closed call: retry the full owned kill; release the retained lease only if verified
            rep = w.terminate(grace_s=3.0)
            acted += 1
            if not rep.get("alive_after"):
                w.cleanup_unconfirmed = False
                fn, w.deferred_finalize = w.deferred_finalize, None
                w.close()
                with contextlib.suppress(Exception):
                    if fn is not None:
                        fn()
                lines.append(f"  owned work {w.id}: cleanup now verified; retained lease released")
            else:
                lines.append(f"  owned work {w.id}: STILL alive after retry ({rep['alive_after'][:5]}); lease kept, operator action needed")
            continue
        if w.worker is None:
            # not a deadline-supervised call (tests / future callers): no way to know it is over → leave it
            lines.append(f"  owned work {w.id}: no supervising worker recorded; not touched")
            continue
        alive = w.alive()
        if not alive:
            w.close()   # worker gone, nothing alive: a leaked registry entry
            continue
        rep = w.terminate(grace_s=3.0)   # worker gone but owned processes survive: safe to stop them
        acted += 1
        lines.append(f"  owned work {w.id}: worker gone, terminated pgids={rep['pgids_killed']} daemon={(rep.get('daemon') or {}).get('stopped')} "
                     f"alive_after={len(rep.get('alive_after') or [])}")
    for p in LS.pending_finalizations():
        if not p["process_alive"]:
            lines.append(f"  stale pending record from dead pid {p['pid']} (owner {p['owner']}): flock already released by the kernel; cleared")
            LS.clear_pending_for_pid(int(p["pid"]))
            acted += 1
        else:
            lines.append(f"  pending late finalization in live pid {p['pid']} (owner {p['owner']}, call {p['tool_call_id']}): "
                         "worker still inside upstream; it will release itself. Not forced.")
    # Per-task breaker state and host circuit: the operator asked for recovery, so a chat locked out
    # by an earlier failure streak (stable session key) gets the browser back now, not after the window.
    cleared = BR.clear_task_state()
    if any(cleared.values()):
        lines.append("  breaker: cleared " + ", ".join(f"{dep} ({n} task record(s))" for dep, n in cleared.items() if n) + "; host circuits closed")
        acted += 1
    q = LS.prune_queue(BROWSER_DEPENDENCY)
    if q.get("pruned"):
        lines.append(f"  lease queue: pruned {q['pruned']} stale waiter ticket(s) of dead processes ({q['live']} live)")
        acted += 1
    if not acted:
        lines.append("  nothing to recover")
    lines.append("Browser lease now: " + (", ".join(h.get("owner", "?") for h in LS.holders(BROWSER_DEPENDENCY)) or "free")
                 + f"; waiters queued: {LS.queue_depth(BROWSER_DEPENDENCY)}")
    return "\n".join(lines)


def _cli_setup(sub) -> None:
    sub.add_argument("--json", action="store_true", help="machine-readable snapshot")


def _cli_handler(ns) -> int:
    if getattr(ns, "json", False):
        print(json.dumps({"policy": _settings(), "lease": LS.holders(BROWSER_DEPENDENCY),
                          "breakers": {d: BR.snapshot(d) for d in BR.list_dependencies()},
                          "active": LG.Ledger().list_active()}, indent=2, default=str))
    else:
        print(status_text())
    return 0


# ---------------------------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------------------------

def register(ctx) -> None:
    global _ctx, _notifier
    _ctx = ctx
    ctx.register_middleware("tool_execution", _tool_execution_middleware)
    ctx.register_hook("subagent_start", _on_subagent_start)
    ctx.register_hook("subagent_stop", _on_subagent_stop)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("agent_loop_stopped", _on_agent_loop_stopped)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    ctx.register_command("guardrails", _cmd_guardrails,
                         description="Show latency/resource guardrail policy, lease, breaker and task status "
                                     "(`/guardrails recover` = ownership-verified recovery)")
    with contextlib.suppress(Exception):
        ctx.register_cli_command("guardrails", "Latency guardrails status", _cli_setup, _cli_handler,
                                 description="Effective policy, browser lease, breakers, active origin tasks")
    # The ownership seam: upstream's one browser-use spawn function, replaced by an equivalent that
    # registers the real Popen with the call that owns it (overlay patch 004 pins the upstream body).
    if not OW.install_spawn_seam():
        logger.error("guardrails: ownership seam NOT installed — deadline kills are disabled (no heuristics fallback)")
    cfg = _settings()
    # Each runtime is one tenant/container; group runtimes never register this.
    # Force progress to the immutable conversation, never a caller supplied JID.
    from ..config import RuntimeConfig
    runtime_scope = RuntimeConfig.from_env()
    cfg["notify_chats"] = [runtime_scope.conversation_id]
    cfg["notify_groups"] = False
    # Startup reconciliation: ledgers left `running` by a process that died (SIGKILL/OOM/restart)
    # are retired here so the notifier never inherits a phantom in-flight entry from a previous life.
    # Skipped under pytest like the ticker below: tests drive ledgers with fake clocks, and a real-time
    # sweep would retire them as "stale" (reconcile_all itself is unit-tested in test_progress.py).
    if "PYTEST_CURRENT_TEST" not in os.environ:
        with contextlib.suppress(Exception):
            retired = LG.Ledger().reconcile_all(max_age_s=float(cfg["notify_max_age_s"]))
            if retired:
                logger.warning("guardrails: retired %d leftover ledger(s) at startup: %s", len(retired), retired[:5])
    # The background ticker is the piece that works while the model/tool blocks. Under pytest it is
    # started only by tests that ask for it (test_progress_ticker.py drives Notifier.start directly).
    if cfg.get("notify", True) and "PYTEST_CURRENT_TEST" not in os.environ and _notifier is None:
        _notifier = PG.Notifier(LG.Ledger(), send=PG.send_via_hermes, first_after_s=float(cfg["notify_first_after_s"]),
                                every_s=float(cfg["notify_every_s"]), max_age_s=float(cfg["notify_max_age_s"]),
                                scope=_notice_scope(cfg))
        _notifier.start(interval_s=15.0)
        with contextlib.suppress(Exception):
            ctx.on_unload(_notifier.stop)
    logger.info("latency guardrails active: %s", {k: cfg[k] for k in ("enforcement", "browser_capacity", "lease_wait_s",
                                                                       "consecutive_limit", "recovery_budget_s",
                                                                       "resource_scope")})
