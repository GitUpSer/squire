"""Progress notices are scoped to the CURRENT logical task, not the lifetime of the chat ledger.

Regression for the "180/186h Still working" notices: one ledger per chat is reused across days,
so ``compose`` must measure elapsed time and count children / tool errors from the start of the
task now in flight — never from the ledger's ``created_at`` and never across earlier tasks.

Fake clock + local sink. Nothing here touches a real chat or the live ledger directory.
"""

from __future__ import annotations

import pytest

from squire_overlay.guardrails import ledger as LG
from squire_overlay.guardrails import progress as PG


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


A = {"platform": "whatsapp", "chat_id": "111@s.whatsapp.net", "session_key": "agent:main:whatsapp:dm:111"}

DAY = 86_400.0


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def sink():
    sent = []
    return sent, (lambda origin, text, **kw: sent.append((origin["chat_id"], text)) or {"ok": True})


def _run_task(led, clock, *, task_id, tool_call_id, work_s=60.0, status="ok"):
    led.turn_started(A, task_id=task_id, turn_id=f"turn-{task_id}", session_id="s1")
    led.tool_started(A, task_id=task_id, tool_call_id=tool_call_id, tool_name="browser_exec", session_id="s1")
    clock.t += work_s
    led.tool_finished(A, tool_call_id=tool_call_id, status=status, duration_s=work_s)
    led.turn_finished(A, turn_id=f"turn-{task_id}")
    led.task_finished(A, task_id=task_id, status="completed")


def test_elapsed_is_measured_from_the_current_task_not_ledger_creation(hermes_env, clock, sink):
    """Chat ledger created 3 days ago; a new task started 130 s ago must report ~130 s, not ~72 h."""
    sent, send = sink
    led = LG.Ledger(now=clock)
    n = PG.Notifier(led, send=send, now=clock, first_after_s=120, every_s=300)
    _run_task(led, clock, task_id="old", tool_call_id="old-1")
    clock.t += 3 * DAY
    led.turn_started(A, task_id="new", turn_id="turn-new", session_id="s2")
    led.tool_started(A, task_id="new", tool_call_id="new-1", tool_name="browser_exec", session_id="s2")
    clock.t += 130
    n.tick()
    assert len(sent) == 1
    text = sent[0][1]
    assert "130s elapsed" in text, text
    assert "h elapsed" not in text, text


def test_new_task_after_completion_does_not_report_old_children_or_errors(hermes_env, clock, sink):
    """Yesterday's task had 2 children (1 failed) and 3 tool errors. Today's notice must not mention them."""
    sent, send = sink
    led = LG.Ledger(now=clock)
    n = PG.Notifier(led, send=send, now=clock, first_after_s=120, every_s=300)
    led.child_started(A, parent_task_id="root", child_task_id="k1", goal="Old child one")
    led.child_started(A, parent_task_id="root", child_task_id="k2", goal="Old child two")
    led.child_finished(A, child_task_id="k1", status="failed")
    led.child_finished(A, child_task_id="k2", status="completed")
    for i in range(3):
        _run_task(led, clock, task_id="old", tool_call_id=f"old-{i}", status="error")
    clock.t += DAY
    led.turn_started(A, task_id="new", turn_id="turn-new", session_id="s2")
    clock.t += 130
    n.tick()
    assert len(sent) == 1
    text = sent[0][1]
    assert "sub-tasks" not in text, text
    assert "tool error" not in text, text
    assert "Old child" not in text, text
    assert "130s elapsed" in text


def test_concurrent_siblings_share_one_task_scope_and_one_notice(hermes_env, clock, sink):
    """Two sibling subagents of the same request: one finishing must not reset the task timer, and the
    chat gets ONE aggregated notice that counts both siblings."""
    sent, send = sink
    led = LG.Ledger(now=clock)
    n = PG.Notifier(led, send=send, now=clock, first_after_s=120, every_s=300)
    led.turn_started(A, task_id="root", turn_id="t-root", session_id="s1")
    led.child_started(A, parent_task_id="root", child_task_id="samsung", goal="Samsung")
    led.child_started(A, parent_task_id="root", child_task_id="pixel", goal="Pixel")
    led.tool_started(A, task_id="samsung", tool_call_id="a1", tool_name="browser_exec", session_id="sa")
    led.tool_started(A, task_id="pixel", tool_call_id="b1", tool_name="browser_exec", session_id="pb")
    clock.t += 60
    led.tool_finished(A, tool_call_id="a1", status="error", reason="deadline_exceeded", duration_s=60)
    led.child_finished(A, child_task_id="samsung", status="failed", summary="deadline")
    clock.t += 70
    assert n.tick() == 1
    text = sent[0][1]
    assert "130s elapsed" in text, text
    assert "Pixel" in text and "1/2 sub-tasks finished (1 failed)" in text and "1 tool error(s)" in text, text
    assert led.snapshot(A)["task"]["children"] == 2


def test_muted_chat_stays_muted_when_a_new_task_starts(hermes_env, clock, sink):
    """Operator muted a chat by blanking origin.chat_id in its ledger. A later task arriving with the
    full origin must NOT restore the chat id or notify — the scope reset is stats-only."""
    sent, send = sink
    led = LG.Ledger(now=clock)
    n = PG.Notifier(led, send=send, now=clock, first_after_s=120, every_s=300)
    _run_task(led, clock, task_id="old", tool_call_id="old-1")
    with led._locked(A) as d:
        d["origin"]["chat_id"] = ""                                      # deliberate mute
    clock.t += DAY
    led.turn_started(A, task_id="new", turn_id="turn-new", session_id="s2")
    led.tool_started(A, task_id="new", tool_call_id="new-1", tool_name="browser_exec", session_id="s2")
    clock.t += 500
    assert n.tick() == 0 and sent == []
    snap = led.snapshot(A)
    assert snap["origin"]["chat_id"] == "" and snap["status"] == "running"
    assert snap["task"]["started_at"] == clock.t - 500, "stats still scoped to the new task"


def test_legacy_ledger_without_task_block_measures_from_oldest_in_flight(hermes_env, clock):
    """Ledgers written before this change have no ``task`` block: fall back to the oldest in-flight
    entry, never to created_at."""
    led = LG.Ledger(now=clock)
    led.tool_started(A, task_id="root", tool_call_id="c1", tool_name="browser_exec", session_id="s1")
    with led._locked(A) as d:
        d.pop("task", None)
        d["created_at"] = clock.t - 3 * DAY
        d["counters"]["tool_errors"] = 40
    clock.t += 130
    text = PG.compose(led.snapshot(A), clock.t)
    assert "130s elapsed" in text and "tool error" not in text, text


def test_task_retired_for_age_keeps_its_scope_on_reactivation(hermes_env, clock, sink):
    """A 3 h task with notify_max_age_s = 2 h: the notifier retires the ledger (``stale``). The task's
    next tool call must NOT open a fresh scope — otherwise the owner reads "⏳ 1m elapsed" for a
    2-hour-old task and the notices continue forever in max_age-sized chunks (review 2026-10-18)."""
    sent, send = sink
    led = LG.Ledger(now=clock)
    n = PG.Notifier(led, send=send, now=clock, first_after_s=120, every_s=300, max_age_s=7200)
    t0 = clock.t
    led.turn_started(A, task_id="long", turn_id="turn-long", session_id="s1")
    led.tool_started(A, task_id="long", tool_call_id="l1", tool_name="terminal", session_id="s1")
    clock.t += 130
    assert n.tick() == 1
    clock.t += 7200
    assert n.tick() == 0 and led.snapshot(A)["status"] == "stale"
    # the same task keeps going: its next tool call reactivates the ledger
    led.tool_started(A, task_id="long", tool_call_id="l2", tool_name="terminal", session_id="s1")
    snap = led.snapshot(A)
    assert snap["status"] == "running" and snap["task"]["started_at"] == t0, "scope kept, clock not reset"
    clock.t += 400
    assert n.tick() == 0 and len(sent) == 1, "retired for age stays retired"
    assert led.snapshot(A)["status"] == "stale"
    # when that task finally ends, the chat's NEXT task gets a fresh scope and a fresh notice
    led.task_finished(A, task_id="long", status="completed")
    clock.t += 60
    led.tool_started(A, task_id="next", tool_call_id="n1", tool_name="terminal", session_id="s2")
    clock.t += 130
    assert n.tick() == 1 and "130s elapsed" in sent[-1][1], sent[-1][1]


def test_abandoned_ledger_opens_a_fresh_scope(hermes_env, clock, sink):
    """``abandoned`` means the process died (gateway restart): whatever comes next IS a new task."""
    sent, send = sink
    led = LG.Ledger(now=clock)
    n = PG.Notifier(led, send=send, now=clock, first_after_s=120, every_s=300)
    led.tool_started(A, task_id="crashed", tool_call_id="c1", tool_name="terminal", session_id="s1")
    with led._locked(A) as d:
        for e in d["in_flight"].values():
            e["pid"] = 2 ** 22 + 7                                        # certainly not alive
    clock.t += 3 * DAY
    assert n.tick() == 0 and led.snapshot(A)["status"] == "abandoned"
    led.tool_started(A, task_id="fresh", tool_call_id="f1", tool_name="terminal", session_id="s2")
    assert led.snapshot(A)["task"]["started_at"] == clock.t
    clock.t += 130
    assert n.tick() == 1 and "130s elapsed" in sent[0][1], sent[0][1]
