"""Automatic, bounded user progress notification (acceptance item 5).

A ``Notifier.tick()`` scans every RUNNING ledger. For an origin whose oldest in-flight tool
(any sibling task) has been running for ``first_after_s`` (default 120 s) and that has not
been notified in the last ``every_s`` (default 300 s), it sends ONE aggregated message to the
origin chat and records it in the ledger — so a restart, a second process, or two concurrent
tickers cannot double-send (the ledger write happens under the same lock as the due-check).

Delivery goes through the injected ``send(origin, text)``; production wires it to Hermes'
``send_message_tool`` with ``"<platform>:<chat_id>"`` from the origin captured at dispatch
time (existing gateway routing, no hard-coded chat id). Origins without a chat id (CLI, cron,
API) are ledger-only. The text never claims success: the task is still running.

Scope: the notice is an OWNER-DM courtesy, not a broadcast. ``NoticeScope`` decides per origin,
on the fresh locked read: only a DM (upstream's ``:dm:`` chat_type slot in the session key, and
not a WhatsApp group/broadcast/newsletter JID) qualifies; groups, channels, threads and anything
unknown are ledger-only unless ``notify_groups`` is explicitly true. A non-empty ``notify_chats``
allow-list restricts notices to exactly those chats (exact id, bare part before ``@``, or a
WhatsApp LID/phone twin of a listed id). An excluded origin never gets a notification record
either — the ledger is untouched.

Bounded: every tick first reconciles the ledger (``Ledger.reconcile_data``): entries whose process
is dead are dropped (a SIGKILLed / OOM-killed gateway never ran ``tool_finished``) and a ledger whose
oldest in-flight entry is older than ``max_age_s`` is marked ``stale``. Either way the ledger
leaves ``running`` and the notifier goes quiet — it can never message a chat indefinitely.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Iterable, Optional

from . import ledger as LG

logger = logging.getLogger(__name__)

DEFAULT_FIRST_AFTER_S = 120.0
DEFAULT_EVERY_S = 300.0
# No single tool call (max timeout 1800 s + allowance) or single LLM call can legitimately be in
# flight this long; beyond it the entry is a leftover and the notifier must stop.
DEFAULT_MAX_AGE_S = 2 * 3600.0


# WhatsApp JIDs that are chats but never a person (same tuple hermes_personal.ignored_chats uses).
# ``status@broadcast`` would otherwise be a "DM" and the heartbeat would be published as a Status.
_WA_NON_USER_SUFFIXES = ("@g.us", "@broadcast", "@newsletter")


def _bare(chat_id: str) -> str:
    return str(chat_id or "").split("@", 1)[0]


def _chat_type_slot(session_key: str) -> str:
    """The ``chat_type`` slot of an upstream session key, or "" when the key is not one.
    Upstream layout (gateway/session.py ``build_session_key``): ``<ns>:<platform>:<chat_type>[…]``
    where ``<ns>`` is always ``agent:<profile>`` (``agent:main`` by default), so the slot is
    ``parts[3]``. Values upstream emits: dm, group, channel, thread, forum, guild, webhook."""
    parts = str(session_key or "").split(":")
    if len(parts) > 3 and parts[0] == "agent":
        return parts[3]
    return ""


def is_dm_origin(origin: dict) -> bool:
    """A direct chat with one person — the ONLY kind of origin that may get an automatic notice.

    Decided from upstream's own ``:dm:`` chat_type slot rather than from a list of group markers,
    so channels, threads, forums and any chat type added later are non-DM by construction. A key
    without the slot (``_current_origin`` synthesizes ``<platform>:<chat_id>`` when
    HERMES_SESSION_KEY is absent in that context) is non-DM too: fail closed, a missed courtesy
    beats a notice in a public channel. WhatsApp group / broadcast / newsletter JIDs are never DMs
    whatever the key says."""
    origin = origin or {}
    chat_id = str(origin.get("chat_id") or "")
    if not chat_id or chat_id.lower().endswith(_WA_NON_USER_SUFFIXES):
        return False
    return _chat_type_slot(str(origin.get("session_key") or "")) == "dm"


def is_group_origin(origin: dict) -> bool:
    """Anything that is not a DM: group, channel, thread, broadcast, unknown. The policy/README
    keep calling the opt-in ``notify_groups``; the decision itself lives in ``is_dm_origin``."""
    return not is_dm_origin(origin)


class NoticeScope:
    """Who may receive an automatic progress notice.

    ``notify_groups`` (default False): every non-DM origin is structurally excluded; nobody in a
    group or channel asked for a status feed. ``notify_chats`` (default empty = every DM with a
    chat id): when set, ONLY these chats qualify — exact chat id, the bare part before ``@``, or
    (WhatsApp) a LID/phone twin of a listed id resolved through the bridge's lid-mapping files;
    ints from an unquoted YAML key are tolerated. Both rules apply: an allow-listed group is still
    excluded unless ``notify_groups`` is true."""

    def __init__(self, *, notify_chats: Optional[Iterable] = None, notify_groups: bool = False):
        self.notify_groups = bool(notify_groups)
        configured: list[str] = []
        ids: set[str] = set()
        for c in (notify_chats or []):
            s = str(c).strip()
            if not s:
                continue
            configured.append(s)
            ids.add(s)
            bare = _bare(s)
            if bare:                           # a stray "@lid" must not add "" (matches any "@…" id)
                ids.add(bare)
        self.configured = tuple(configured)    # as written in config: what /guardrails shows
        self.allowed_ids = frozenset(ids)

    def _listed(self, origin: dict) -> bool:
        chat_id = str(origin.get("chat_id") or "")
        if chat_id in self.allowed_ids or _bare(chat_id) in self.allowed_ids:
            return True
        if str(origin.get("platform") or "") == "whatsapp":
            # The bridge may hand us the phone JID one day and the @lid JID the next for the same
            # person; the allow-list names one of them. Same helper ignored_chats uses (upstream
            # gateway.whatsapp_identity, lid-mapping files); degrades to no match when unavailable.
            try:
                from ..ignored_chats import _matches_whatsapp_alias
                return bool(_matches_whatsapp_alias(chat_id, set(self.configured)))
            except Exception:  # noqa: BLE001 - alias resolution is best-effort
                return False
        return False

    def allows(self, origin: dict) -> bool:
        origin = origin or {}
        if not str(origin.get("chat_id") or ""):
            return False                       # CLI / cron / API / muted: ledger-only
        if not is_dm_origin(origin) and not self.notify_groups:
            return False
        if self.allowed_ids and not self._listed(origin):
            return False
        return True


def _fmt_s(x: float) -> str:
    return f"{int(round(x))}s" if x < 3600 else f"{x / 3600:.1f}h"


def compose(data: dict, now: float) -> str:
    inflight = sorted(data.get("in_flight", {}).values(), key=lambda e: e.get("started_at", now))
    task = LG.task_scope(data, now)
    t_start = float(task["started_at"])
    # only this task's children: anything started before the current scope belongs to an earlier request
    children = {k: c for k, c in (data.get("children") or {}).items()
                if c.get("status") == "running" or float(c.get("started_at", t_start)) >= t_start}
    blockers = data.get("blockers", {})
    lines = [f"⏳ Still working — {_fmt_s(now - t_start)} elapsed, not finished yet."]
    for e in inflight[:4]:
        tid = e.get("task_id", "")
        label = children.get(tid, {}).get("goal") or (f"task {tid}" if tid and tid != "root" else "main task")
        line = f"• {label}: {e.get('tool', '?')} running {_fmt_s(now - float(e.get('started_at', now)))}"
        b = blockers.get(tid)
        if b:
            line += f" — blocked: {b.get('code')} ({b.get('detail')})" if b.get("detail") else f" — blocked: {b.get('code')}"
        lines.append(line)
    done = [c for c in children.values() if c.get("status") not in (None, "running")]
    if done:
        lines.append(f"• {len(done)}/{len(children)} sub-tasks finished "
                     f"({sum(1 for c in done if c.get('status') == 'failed')} failed)")
    errors = int(task.get("tool_errors", 0) or 0)
    if errors:
        lines.append(f"• {errors} tool error(s) so far")
    lines.append("No action needed; you'll get the result or a blocker report when it ends.")
    return "\n".join(lines)


class Notifier:
    def __init__(self, ledger: LG.Ledger, *, send: Callable[..., dict], now: Callable[[], float] = time.time,
                 first_after_s: float = DEFAULT_FIRST_AFTER_S, every_s: float = DEFAULT_EVERY_S,
                 max_age_s: float = DEFAULT_MAX_AGE_S, scope: Optional[NoticeScope] = None):
        self.ledger, self.send, self.now = ledger, send, now
        self.first_after_s, self.every_s = float(first_after_s), float(every_s)
        self.max_age_s = float(max_age_s)
        self.scope = scope if scope is not None else NoticeScope()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def _due(self, data: dict, t: float) -> bool:
        if data.get("status") != "running" or not self.scope.allows(data.get("origin") or {}):
            return False
        inflight = data.get("in_flight") or {}
        if not inflight:
            return False
        oldest = min(float(e.get("started_at", t)) for e in inflight.values())
        if t - oldest < self.first_after_s:
            return False
        if self.max_age_s > 0 and t - oldest > self.max_age_s:
            return False   # leftover entry (reconcile marks the ledger stale); never keep notifying
        notes = data.get("notifications") or []
        if notes and t - float(notes[-1].get("at", 0)) < self.every_s:
            return False
        return True

    def tick(self) -> int:
        """Scan and send what is due. Returns number of messages attempted."""
        sent = 0
        t = self.now()
        for data in self.ledger.list_active():
            origin = data.get("origin") or {}
            # Re-check + record under the ledger lock so two tickers cannot both send.
            with self.ledger._locked(origin) as fresh:
                # crash / staleness hygiene first: a dead process's entries or an over-age ledger
                # take the ledger out of "running" here, under the same lock as the due-check
                if LG.Ledger.reconcile_data(fresh, t, max_age_s=self.max_age_s) or not self._due(fresh, t):
                    continue
                # Deliver to the origin that was just checked, not the pre-lock listing's copy.
                origin = dict(fresh.get("origin") or origin)
                text = compose(fresh, t)
                fresh["notifications"] = (fresh.get("notifications") or [])[-19:] + [
                    {"at": t, "delivered": None, "error": "", "chars": len(text)}]
            delivered, err = False, ""
            try:
                res = self.send(origin, text)
                delivered = bool(res is None or (isinstance(res, dict) and res.get("ok", True)))
                err = "" if delivered else str((res or {}).get("error", ""))[:200]
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"[:200]
            with self.ledger._locked(origin) as fresh:
                if fresh.get("notifications"):
                    fresh["notifications"][-1].update({"delivered": delivered, "error": err})
            sent += 1
        return sent

    # -- background ticker (production) --------------------------------------------------
    def start(self, interval_s: float = 15.0) -> None:
        if self._thread is not None:
            return

        def loop():
            while not self._stop.wait(interval_s):
                try:
                    self.tick()
                except Exception:
                    logger.debug("progress tick failed", exc_info=True)

        self._thread = threading.Thread(target=loop, name="guardrails-progress", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


def send_via_hermes(origin: dict, text: str, **_) -> dict:
    """Durable, private-conversation-only broker egress; no transport credentials."""
    from ..config import RuntimeConfig
    from ..store import Store
    import uuid
    scope = RuntimeConfig.from_env()
    if (scope.kind != "dm" or origin.get("platform") != "squire"
            or origin.get("chat_id") != scope.conversation_id or not is_dm_origin(origin)):
        return {"ok": False, "error": "progress outside private runtime scope"}
    Store(scope.database).enqueue({"conversation_id": scope.conversation_id,
        "response_key": "progress:" + str(uuid.uuid4()), "text": text[:32000],
        "priority": "interactive"})
    return {"ok": True}
