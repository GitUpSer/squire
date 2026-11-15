import asyncio
import json
import os
from dataclasses import replace
from io import BytesIO
import pytest
from squire_overlay.store import Store,ScopeError,QueueFull


def event(scope,identifier="one"):
    return {"event_id":identifier,"conversation_id":scope.conversation_id,"tenant_id":scope.tenant_id,"runtime_id":scope.runtime_id,"chat_type":scope.kind,"sender_id":"sender-hash","provider_message_id":"provider-"+identifier,"timestamp":12345.0,"payload":{"text":"Hello"}}


def test_commit_dedup_scope_and_conflict(scope):
    store=Store(scope.database);payload=event(scope)
    assert store.accept([payload],scope)==["one"]
    assert Store(scope.database).accept([payload],scope)==["one"]
    conflicting={**payload,"payload":{"text":"different"}}
    with pytest.raises(ScopeError):store.accept([conflicting],scope)
    with pytest.raises(ScopeError):store.accept([{**payload,"conversation_id":"other"}],scope)
    assert store.claim()==payload
    assert store.claim() is None


def test_restart_never_reexecutes_uncertain_tool(scope):
    store=Store(scope.database);store.accept([event(scope),event(scope,"two")],scope)
    store.claim();assert store.tool_start("terminal",{"command":"external action"},"call1")
    restarted=Store(scope.database)
    assert restarted.recover()==["one"]
    assert restarted.claim() is None
    assert restarted.resolve("one")
    assert restarted.claim()["event_id"]=="two"


def test_batch_rollback_on_one_invalid_scope(scope):
    store=Store(scope.database)
    with pytest.raises(ScopeError):store.accept([event(scope),{**event(scope,"two"),"tenant_id":"different"}],scope)
    assert store.claim() is None


def test_durable_outbound_keys_cannot_change_content(scope):
    store=Store(scope.database)
    payload={"response_key":"response","conversation_id":scope.conversation_id,"text":"hello"}
    store.enqueue(payload);store.enqueue(payload)
    with pytest.raises(ScopeError):store.enqueue({**payload,"text":"wrong"})
    assert Store(scope.database).outbox()["response_key"]=="response"


def test_http_ack_requires_scope_and_persisted_batch(scope):
    from squire_overlay.http import handle
    class Handler:
        headers={"Authorization":"Bearer "+scope.ingress_token}
        close_connection=False
        def _respond(self,status,payload):self.result=(status,payload)
    handler=Handler();body=json.dumps({"events":[event(scope)]}).encode();handler.headers["Content-Length"]=str(len(body));handler.rfile=BytesIO(body)
    assert handle(handler,"/events")
    assert handler.result[0]==202 and handler.result[1]["accepted"]
    assert Store(scope.database).claim()["event_id"]=="one"
    handler.headers["Authorization"]="Bearer "+scope.egress_token
    handle(handler,"/events");assert handler.result[0]==401


def test_real_plugin_registration_and_named_session(hermes,scope):
    plugin=hermes._plugins.get("squire-overlay")
    assert plugin and plugin.enabled and plugin.error is None, getattr(plugin,"error",None)
    from gateway.platform_registry import platform_registry
    from gateway.config import Platform,PlatformConfig
    from gateway.session import SessionSource,build_session_key
    assert platform_registry.is_registered("squire")
    source=SessionSource(platform=Platform("squire"),chat_id=scope.conversation_id,chat_type="dm")
    assert build_session_key(source)=="agent:main:squire:dm:conversation-a"
    source.chat_type="group";source.user_id="member-one"
    assert build_session_key(source,group_sessions_per_user=False)=="agent:main:squire:group:conversation-a"
    assert {"pre_tool_call","post_tool_call"}<=set(plugin.hooks_registered)


def test_real_group_tools_block_private_access(hermes,scope,monkeypatch):
    monkeypatch.setenv("SQUIRE_RUNTIME_KIND","group")
    from hermes_cli.plugins import _reset_plugin_managers_for_tests,discover_plugins,get_pre_tool_call_directive
    _reset_plugin_managers_for_tests();discover_plugins(force=True)
    for tool in ("terminal","browser","read_file","send_message","delegate_task","execute_code"):
        action,message=get_pre_tool_call_directive(tool,{})
        assert action=="block",tool
    from squire_overlay.adapter import SquireAdapter
    from gateway.config import PlatformConfig
    assert SquireAdapter(PlatformConfig()).toolsets_for_source(None)==["memory","squire-group"]


@pytest.mark.asyncio
async def test_real_adapter_processing_completion_and_scoped_outbox(hermes,scope,monkeypatch):
    from squire_overlay.adapter import SquireAdapter
    from gateway.config import PlatformConfig
    adapter=SquireAdapter(PlatformConfig(extra={"group_sessions_per_user":False}))
    monkeypatch.setattr(adapter.broker,"flush",lambda store:False)
    monkeypatch.setattr(adapter.broker,"request",lambda *args,**kwargs:{"approvals":[]})
    seen=[]
    async def handler(message):seen.append(message.text);return "A real adapter reply"
    adapter.set_message_handler(handler)
    adapter.store.accept([event(scope)],scope)
    await adapter.connect()
    for _ in range(100):
        with adapter.store.db() as db:state=db.execute("SELECT state FROM inbox").fetchone()[0]
        if state=="done":break
        await asyncio.sleep(.05)
    await adapter.disconnect()
    assert state=="done"
    assert seen==["Hello"]
    payload=json.loads(adapter.store.outbox()["payload"])
    assert payload["conversation_id"]==scope.conversation_id
    assert payload["cause_event_id"]=="one"
    assert payload["text"]=="A real adapter reply"
    result=await adapter.send("another-conversation","wrong recipient")
    assert not result.success
