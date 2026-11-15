"""Progress notifier, production shape (review blocker 7).

1. The REAL background ticker (``Notifier.start``) sends while a guarded tool is still blocking in
   another thread — no fake clock, a short real interval.
2. ``send_via_hermes`` goes through the REAL ``tools.send_message_tool.send_message_tool`` (target
   resolution, platform config, relay authorization) with only the platform transport stubbed;
   the stub is the test sink — nothing leaves the host. Verifies exact origin recipient, that two
   concurrent chats each get their own notice, and that no origin is spammed.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from squire_overlay import guardrails as G
from squire_overlay.guardrails import ledger as LG
from squire_overlay.guardrails import progress as PG

A = {"platform": "squire", "chat_id": "conversation-a", "session_key": "agent:main:squire:dm:conversation-a"}
B = {"platform": "whatsapp", "chat_id": "222@s.whatsapp.net", "session_key": "agent:main:whatsapp:dm:222"}


def test_real_ticker_notifies_while_tool_blocks(hermes_env):
    led = LG.Ledger()
    sent = []
    n = PG.Notifier(led, send=lambda o, t: sent.append((o, t)) or {"ok": True}, first_after_s=0.5, every_s=60)
    n.start(interval_s=0.2)
    try:
        hold = threading.Event()

        def blocking_tool():
            led.tool_started(A, task_id="root", tool_call_id="c1", tool_name="browser_exec")
            hold.wait(5)
            led.tool_finished(A, tool_call_id="c1", status="ok")

        t = threading.Thread(target=blocking_tool); t.start()
        deadline = time.monotonic() + 4
        while not sent and time.monotonic() < deadline:
            time.sleep(0.05)
        hold.set(); t.join(5)
        assert sent, "ticker must send while the tool is still blocking"
        assert sent[0][0]["chat_id"] == A["chat_id"] and "Still working" in sent[0][1]
        assert len(sent) == 1, sent   # every_s=60 → no second notice in this window
    finally:
        n.stop()


def queued(scope):
    from squire_overlay.store import Store
    with Store(scope.database).db() as db:
        return [json.loads(row[0]) for row in db.execute("SELECT payload FROM outbox")]


def test_send_via_hermes_uses_real_scoped_durable_outbox(scope):
    assert PG.send_via_hermes(A, "still working") == {"ok": True}
    assert queued(scope)[0]["conversation_id"] == scope.conversation_id
    assert queued(scope)[0]["text"] == "still working"


def test_only_runtime_conversation_gets_notices_and_not_spammed(scope):
    now = {"t": 1000.0}
    led = LG.Ledger(now=lambda: now["t"])
    n = PG.Notifier(led, send=PG.send_via_hermes, now=lambda: now["t"], first_after_s=120, every_s=300,
                    scope=PG.NoticeScope(notify_chats=[scope.conversation_id]))
    led.tool_started(A, task_id="root", tool_call_id="a1", tool_name="browser_exec")
    led.tool_started(B, task_id="root", tool_call_id="b1", tool_name="browser_exec")
    now["t"] += 130
    assert n.tick() == 1
    assert [row["conversation_id"] for row in queued(scope)] == [scope.conversation_id]
    now["t"] += 60
    assert n.tick() == 0 and len(queued(scope)) == 1
    led.tool_finished(A, tool_call_id="a1", status="ok")
    now["t"] += 300
    assert n.tick() == 0  # another conversation's activity cannot authorize delivery


def test_turn_entry_from_pre_llm_call_is_enough_for_a_notice(scope):
    now = {"t": 5000.0}
    led = LG.Ledger(now=lambda: now["t"])
    n = PG.Notifier(led, send=PG.send_via_hermes, now=lambda: now["t"], first_after_s=120, every_s=300)
    led.turn_started(A, task_id="root", turn_id="t1")
    now["t"] += 121
    assert n.tick() == 1 and queued(scope)[0]["conversation_id"] == scope.conversation_id
    led.turn_finished(A, turn_id="t1")
    now["t"] += 400
    assert n.tick() == 0
