"""Harness-owned task ledger + automatic progress notification (acceptance item 5).

Fake clock + local sink. Nothing here touches a real chat.
"""

from __future__ import annotations

import json

import pytest

from squire_overlay.guardrails import ledger as LG
from squire_overlay.guardrails import progress as PG


class Clock:
    def __init__(self, t=10_000.0):
        self.t = t

    def __call__(self):
        return self.t


ORIGIN_A = {"platform": "whatsapp", "chat_id": "111@s.whatsapp.net", "session_key": "agent:main:whatsapp:dm:111"}
ORIGIN_B = {"platform": "whatsapp", "chat_id": "222@s.whatsapp.net", "session_key": "agent:main:whatsapp:dm:222"}


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def sink():
    sent = []
    return sent, (lambda origin, text, **kw: sent.append((origin["chat_id"], text)) or {"ok": True})


def test_ledger_auto_created_on_first_tool_start_and_tracks_lifecycle(hermes_env, clock):
    led = LG.Ledger(now=clock)
    led.tool_started(ORIGIN_A, task_id="root", tool_call_id="c1", tool_name="browser_exec", session_id="s1")
    path = hermes_env / "guardrails" / "ledger" / (LG.task_key(ORIGIN_A) + ".json")
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["origin"]["chat_id"] == ORIGIN_A["chat_id"]
    assert data["in_flight"]["c1"]["tool"] == "browser_exec"
    clock.t += 30
    led.tool_finished(ORIGIN_A, tool_call_id="c1", status="error", reason="deadline_exceeded", duration_s=30)
    data = json.loads(path.read_text())
    assert "c1" not in data["in_flight"]
    assert data["events"][-1] == {"at": clock.t, "kind": "tool_end", "tool": "browser_exec", "status": "error",
                                  "reason": "deadline_exceeded", "duration_s": 30, "task_id": "root", "tool_call_id": "c1"}
    assert data["counters"]["tool_calls"] == 1 and data["counters"]["tool_errors"] == 1


def test_child_progress_rolls_into_parent_origin(hermes_env, clock):
    led = LG.Ledger(now=clock)
    led.child_started(ORIGIN_A, parent_task_id="root", child_task_id="kid-1", goal="check Samsung price")
    led.tool_started(ORIGIN_A, task_id="kid-1", tool_call_id="k1", tool_name="browser_exec", session_id="s-kid")
    snap = led.snapshot(ORIGIN_A)
    assert snap["children"]["kid-1"]["goal"] == "check Samsung price" and snap["children"]["kid-1"]["status"] == "running"
    assert snap["in_flight"]["k1"]["task_id"] == "kid-1"
    led.child_finished(ORIGIN_A, child_task_id="kid-1", status="failed", summary="lease timeout")
    assert led.snapshot(ORIGIN_A)["children"]["kid-1"]["status"] == "failed"


def test_no_notification_before_threshold_then_one_with_precise_blocker(hermes_env, clock, sink):
    sent, send = sink
    led = LG.Ledger(now=clock)
    n = PG.Notifier(led, send=send, now=clock, first_after_s=120, every_s=300)
    led.tool_started(ORIGIN_A, task_id="root", tool_call_id="c1", tool_name="browser_exec", session_id="s1")
    led.set_blocker(ORIGIN_A, task_id="root", code="lease_wait", detail="waiting for browser lease held by task samsung")
    clock.t += 119; n.tick()
    assert sent == []
    clock.t += 2; n.tick()
    assert len(sent) == 1 and sent[0][0] == ORIGIN_A["chat_id"]
    text = sent[0][1]
    assert "browser_exec" in text and "121s" in text and "waiting for browser lease held by task samsung" in text
    assert "still working" in text.lower() or "in progress" in text.lower()
    assert "success" not in text.lower() and "done" not in text.lower()
    n.tick(); n.tick()
    assert len(sent) == 1                                  # rate limited
    clock.t += 299; n.tick(); assert len(sent) == 1
    clock.t += 2; n.tick(); assert len(sent) == 2          # periodic


def test_finished_task_never_notifies(hermes_env, clock, sink):
    sent, send = sink
    led = LG.Ledger(now=clock)
    n = PG.Notifier(led, send=send, now=clock, first_after_s=120, every_s=300)
    led.tool_started(ORIGIN_A, task_id="root", tool_call_id="c1", tool_name="browser_exec", session_id="s1")
    clock.t += 60
    led.tool_finished(ORIGIN_A, tool_call_id="c1", status="ok", duration_s=60)
    led.task_finished(ORIGIN_A, task_id="root", status="completed")
    clock.t += 1000; n.tick()
    assert sent == []


def test_sibling_jobs_aggregate_by_origin_not_cross_chat(hermes_env, clock, sink):
    sent, send = sink
    led = LG.Ledger(now=clock)
    n = PG.Notifier(led, send=send, now=clock, first_after_s=120, every_s=300)
    led.child_started(ORIGIN_A, parent_task_id="root", child_task_id="samsung", goal="Samsung")
    led.child_started(ORIGIN_A, parent_task_id="root", child_task_id="pixel", goal="Pixel")
    led.tool_started(ORIGIN_A, task_id="samsung", tool_call_id="a1", tool_name="browser_exec", session_id="s")
    led.tool_started(ORIGIN_A, task_id="pixel", tool_call_id="b1", tool_name="browser_exec", session_id="p")
    led.tool_started(ORIGIN_B, task_id="other", tool_call_id="z1", tool_name="terminal", session_id="z")
    clock.t += 130; n.tick()
    chats = [c for c, _ in sent]
    assert chats.count(ORIGIN_A["chat_id"]) == 1 and chats.count(ORIGIN_B["chat_id"]) == 1
    a_text = dict(sent)[ORIGIN_A["chat_id"]]
    assert "Samsung" in a_text and "Pixel" in a_text and "terminal" not in a_text


def test_persisted_state_prevents_duplicate_after_restart(hermes_env, clock, sink):
    sent, send = sink
    led = LG.Ledger(now=clock)
    PG.Notifier(led, send=send, now=clock, first_after_s=120, every_s=300)
    led.tool_started(ORIGIN_A, task_id="root", tool_call_id="c1", tool_name="browser_exec", session_id="s1")
    clock.t += 130
    PG.Notifier(led, send=send, now=clock, first_after_s=120, every_s=300).tick()
    assert len(sent) == 1
    # "process restart": brand-new objects over the same HERMES_HOME
    led2 = LG.Ledger(now=clock)
    PG.Notifier(led2, send=send, now=clock, first_after_s=120, every_s=300).tick()
    assert len(sent) == 1


def test_non_messaging_origin_is_ledger_only(hermes_env, clock, sink):
    sent, send = sink
    led = LG.Ledger(now=clock)
    n = PG.Notifier(led, send=send, now=clock, first_after_s=120, every_s=300)
    cli = {"platform": "cli", "chat_id": "", "session_key": "cli:abc"}
    led.tool_started(cli, task_id="root", tool_call_id="c1", tool_name="browser_exec", session_id="s1")
    clock.t += 500; n.tick()
    assert sent == [] and led.snapshot(cli)["in_flight"]


def test_send_failure_is_recorded_and_retried_next_period_only(hermes_env, clock):
    calls = []

    def flaky(origin, text, **kw):
        calls.append(text)
        return {"ok": False, "error": "bridge down"}

    led = LG.Ledger(now=clock)
    n = PG.Notifier(led, send=flaky, now=clock, first_after_s=120, every_s=300)
    led.tool_started(ORIGIN_A, task_id="root", tool_call_id="c1", tool_name="browser_exec", session_id="s1")
    clock.t += 130; n.tick(); n.tick()
    assert len(calls) == 1
    assert led.snapshot(ORIGIN_A)["notifications"][-1]["delivered"] is False
    clock.t += 301; n.tick()
    assert len(calls) == 2


def _dead_pid() -> int:
    import subprocess, sys
    p = subprocess.Popen([sys.executable, "-c", "pass"]); p.wait()
    return p.pid


def test_entries_of_a_dead_process_are_reconciled_and_never_notify(hermes_env, clock, sink):
    """A SIGKILLed/OOM-killed process never runs tool_finished. Its entries must not keep the ledger
    `running` (the notifier would message the chat every notify_every_s forever)."""
    sent, send = sink
    led = LG.Ledger(now=clock)
    led.tool_started(ORIGIN_A, task_id="root", tool_call_id="c1", tool_name="browser_exec", session_id="s1")
    with led._locked(ORIGIN_A) as d:
        d["in_flight"]["c1"]["pid"] = _dead_pid()
    n = PG.Notifier(led, send=send, now=clock, first_after_s=120, every_s=300)
    clock.t += 1000
    assert n.tick() == 0 and sent == []
    snap = led.snapshot(ORIGIN_A)
    assert snap["status"] == "abandoned" and snap["in_flight"] == {}
    assert snap["events"][-1]["kind"] == "task_end" and snap["events"][-1]["reason"] == "process_gone"
    clock.t += 1000
    assert n.tick() == 0, "retired ledgers stay quiet"


def test_startup_reconcile_retires_leftovers_but_keeps_live_process_entries(hermes_env, clock):
    led = LG.Ledger(now=clock)
    led.tool_started(ORIGIN_A, task_id="root", tool_call_id="dead", tool_name="browser_exec")
    with led._locked(ORIGIN_A) as d:
        d["in_flight"]["dead"]["pid"] = _dead_pid()
    led.tool_started(ORIGIN_B, task_id="root", tool_call_id="live", tool_name="browser_exec")   # our pid: alive
    assert led.reconcile_all(max_age_s=7200) == [(LG.task_key(ORIGIN_A), "abandoned")]
    assert led.snapshot(ORIGIN_B)["status"] == "running" and "live" in led.snapshot(ORIGIN_B)["in_flight"]


def test_notifications_stop_after_max_age_and_ledger_is_marked_stale(hermes_env, clock, sink):
    sent, send = sink
    led = LG.Ledger(now=clock)
    n = PG.Notifier(led, send=send, now=clock, first_after_s=120, every_s=300, max_age_s=3600)
    led.tool_started(ORIGIN_A, task_id="root", tool_call_id="c1", tool_name="browser_exec", session_id="s1")
    clock.t += 200; n.tick()
    assert len(sent) == 1
    clock.t += 3500; n.tick()           # 3700 s after start: beyond max_age
    assert len(sent) == 1, "no notice past the max age"
    assert led.snapshot(ORIGIN_A)["status"] == "stale"
    clock.t += 1000; n.tick()
    assert len(sent) == 1
