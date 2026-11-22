"""Actual pinned cron delivery seam plus real scoped adapter/outbox."""
import ast
import contextlib
import os
from pathlib import Path
import subprocess
import types
from unittest.mock import patch
import pytest
from squire_overlay.scheduled import delivery_scope,response_key


def test_same_occurrence_new_attempt_preserves_delivery_identity(monkeypatch):
    record={"job_id":"reminder", "scheduled_instant":"2026-11-15T12:00:00+00:00"}
    monkeypatch.setattr("cron.executions.get_execution",lambda _:record)
    with delivery_scope({"id":"reminder","execution_id":"attempt-a"}):a=response_key("due now")
    with delivery_scope({"id":"reminder","execution_id":"attempt-b"}):assert response_key("due now")==a
    with delivery_scope({"id":"reminder","execution_id":"attempt-b"},for_failure=True):assert response_key("due now")!=a
    assert response_key("due now") is None
    with pytest.raises(ValueError):
        with delivery_scope({"id":"other","execution_id":"attempt-a"}):pass
    record["scheduled_instant"]=None
    with delivery_scope({"id":"reminder","execution_id":"manual-a"}):a=response_key("due now")
    with delivery_scope({"id":"reminder","execution_id":"manual-b"}):assert response_key("due now")!=a


@pytest.mark.asyncio
async def test_replayed_cron_live_and_standalone_enqueue_one_operation(hermes,scope,monkeypatch):
    from gateway.config import PlatformConfig
    from squire_overlay.adapter import SquireAdapter,standalone_send
    from squire_overlay.client import Broker
    from squire_overlay.store import Store
    monkeypatch.setattr("cron.executions.get_execution",lambda _: {"job_id":"job", "scheduled_instant":"2026-11-15T12:00:00+00:00"})
    monkeypatch.setattr(Broker,"flush",lambda *args:None)
    with delivery_scope({"id":"job","execution_id":"attempt"}):
        first=await SquireAdapter(PlatformConfig()).send(scope.conversation_id,"Reminder")
    with delivery_scope({"id":"job","execution_id":"attempt"}):
        second=await standalone_send(PlatformConfig(),scope.conversation_id,"Reminder")
    assert first.message_id==second["message_id"]
    with Store(scope.database).db() as db:assert db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]==1


def test_pinned_scheduler_wrapper_scopes_real_delivery(tmp_path,monkeypatch):
    source=Path(os.environ["HERMES_SOURCE"])/"cron/scheduler_delivery.py"
    target=tmp_path/"cron/scheduler_delivery.py";target.parent.mkdir();target.write_text(source.read_text())
    if "SQUIRE_DURABLE_CRON_DELIVERY" not in target.read_text():
        patchfile=Path(__file__).resolve().parents[2]/"patches/007-durable-cron-delivery.patch"
        subprocess.run(["patch","-p1","-i",str(patchfile)],cwd=tmp_path,check=True,capture_output=True)
    node=next(n for n in ast.parse(target.read_text()).body if isinstance(n,ast.FunctionDef) and n.name=="_deliver_result")
    module=ast.Module(body=[ast.ImportFrom(module="__future__",names=[ast.alias(name="annotations")],level=0),node],type_ignores=[])
    seen=[]
    ns={"os":os,"_deliver_result_scoped":lambda *a,**kw:seen.append(response_key(a[1]))}
    exec(compile(ast.fix_missing_locations(module),str(target),"exec"),ns)
    monkeypatch.setenv("SQUIRE_RUNTIME_ID","runtime")
    monkeypatch.setattr("cron.executions.get_execution",lambda _: {"job_id":"job", "scheduled_instant":"same-fire"})
    ns["_deliver_result"]({"id":"job","execution_id":"attempt"},"Reminder")
    ns["_deliver_result"]({"id":"job","execution_id":"attempt"},"Reminder")
    assert seen[0]==seen[1] and seen[0].startswith("scheduled:")
