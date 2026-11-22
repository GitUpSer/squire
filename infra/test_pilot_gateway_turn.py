"""Local contract tests only: real Store/Broker/shim, synthetic model handler."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import tempfile
import time
from http.server import BaseHTTPRequestHandler,HTTPServer
from urllib.error import HTTPError
from urllib.request import Request,urlopen

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"tenant-image/overlay/plugin"))
from squire_overlay.config import RuntimeConfig
from squire_overlay.store import Store
from squire_overlay.client import Broker
from squire_overlay.http import handle

spec=importlib.util.spec_from_file_location("gateway_turn",Path(__file__).with_name("pilot_gateway_turn.py"))
turn=importlib.util.module_from_spec(spec);spec.loader.exec_module(turn)
IMAGE="ghcr.io/fixture/runtime@sha256:"+"a"*64


@pytest.fixture
def scope(tmp_path,monkeypatch):
    monkeypatch.setattr(turn,"HOME",tmp_path)
    values={"RAILWAY_SERVICE_ID":turn.SERVICE,"SQUIRE_RUNTIME_ID":turn.RUNTIME,
            "SQUIRE_OWNER_TENANT_ID":turn.RUNTIME,"SQUIRE_CONVERSATION_ID":turn.CONVERSATION,
            "SQUIRE_RUNTIME_KIND":"dm","SQUIRE_HINDSIGHT_VARIANT":"standard",
            "HOME":str(tmp_path),"HERMES_HOME":str(tmp_path),"SQUIRE_VOLUME":str(tmp_path),
            "SQUIRE_MESSAGING_URL":turn.SINK,"CONTROL_API_URL":turn.SINK,
            "HINDSIGHT_API_WORKER_ENABLED":"false","HINDSIGHT_API_LLM_PROVIDER":"none",
            "SQUIRE_INGRESS_TOKEN":"i"*40,"SQUIRE_EGRESS_TOKEN":"e"*40,
            "SQUIRE_CONTROL_TOKEN":"c"*40,"SQUIRE_DEPLOYMENT_GENERATION":"1",
            "SQUIRE_IMAGE_REF":IMAGE,"ANTHROPIC_API_KEY":"sk-synthetic-virtual-key",
            "ANTHROPIC_BASE_URL":turn.MODEL_BASE}
    for key in list(os.environ):monkeypatch.delenv(key)
    for key,value in values.items():monkeypatch.setenv(key,value)
    (tmp_path/".squire").mkdir()
    (tmp_path/".squire/runtime-profile.json").write_text(json.dumps({"runtime_id":turn.RUNTIME,"conversation_id":turn.CONVERSATION,"kind":"dm"}))
    original=Path.read_text
    def read(path,*args,**kwargs):
        if path==Path("/proc/self/mountinfo"):return "1 2 3 4 "+str(tmp_path)+" 6"
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,"read_text",read)
    return RuntimeConfig.from_env()


@pytest.fixture
def sink(scope,tmp_path,monkeypatch):
    journal=turn.SinkJournal(tmp_path/"sink.sqlite3")
    server=HTTPServer(("127.0.0.1",0),turn.handler(journal,scope.egress_token,scope.control_token))
    base="http://127.0.0.1:"+str(server.server_port)
    monkeypatch.setattr(turn,"SINK",base)
    monkeypatch.setenv("SQUIRE_MESSAGING_URL",base);monkeypatch.setenv("CONTROL_API_URL",base)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    yield base,journal
    server.shutdown();server.server_close();thread.join(5)


def outbound(**changes):
    return {"conversation_id":turn.CONVERSATION,"response_key":turn.RESPONSE_KEY,
            "cause_event_id":turn.EVENT_ID,"text":turn.ANSWER,"priority":"interactive",**changes}


def call(base,path,token,payload=None):
    request=Request(base+path,data=turn.encoded(payload) if payload is not None else None,
                    headers={"Authorization":"Bearer "+token,"Content-Type":"application/json"})
    try:
        with urlopen(request,timeout=3) as response:return response.status,json.load(response)
    except HTTPError as error:return error.code,json.load(error)


@pytest.mark.parametrize("key,value",[("RAILWAY_SERVICE_ID","production"),("SQUIRE_RUNTIME_ID","customer"),
    ("SQUIRE_CONVERSATION_ID","customer"),("SQUIRE_MESSAGING_URL","https://production.invalid"),
    ("CONTROL_API_URL","https://production.invalid"),("HINDSIGHT_API_WORKER_ENABLED","true"),
    ("ANTHROPIC_BASE_URL","https://unrelated-proxy.invalid"),
    ("ANTHROPIC_API_KEY","sk-ant-synthetic-provider-key"),("OPENAI_API_KEY","synthetic-owner-key"),
    ("SQUIRE_IMAGE_REF","mutable-tag")])
def test_guard_rejects_wrong_scope_or_credentials(scope,monkeypatch,key,value):
    monkeypatch.setenv(key,value)
    with pytest.raises(turn.CanaryError):turn.guard(IMAGE,turn.MODEL_BASE)


def test_guard_accepts_exact_canary_and_rejects_owner_auth(scope):
    turn.guard(IMAGE,turn.MODEL_BASE)
    (scope.home/"auth.json").write_text('{"owner":"private"}')
    with pytest.raises(turn.CanaryError,match="owner_credentials"):turn.guard(IMAGE)


@pytest.fixture
def generated_pool(scope):
    with tempfile.TemporaryDirectory(prefix="squire-auth-metadata-test-",dir="/dev/shm") as directory:
        target=Path(directory)/"auth.json"
        path=scope.home/"auth.json";path.symlink_to(target)
        entry={"id":"abc123","label":"ANTHROPIC_API_KEY","auth_type":"api_key","priority":0,
               "source":"env:ANTHROPIC_API_KEY","last_status":None,"last_status_at":None,
               "last_error_code":None,"last_error_reason":None,"last_error_message":None,
               "last_error_reset_at":None,"base_url":turn.MODEL_BASE,"request_count":0,
               "secret_fingerprint":"sha256:"+turn.digest(os.environ["ANTHROPIC_API_KEY"].encode())[:16]}
        data={"version":1,"providers":{},"credential_pool":{"anthropic":[entry]},"updated_at":"2026-11-22T16:50:00.123456+00:00"}
        target.write_text(json.dumps(data))
        yield path,target,data,entry


def test_sealed_env_virtual_key_pool_metadata_is_allowed(scope,generated_pool):
    path,target,data,entry=generated_pool
    assert turn.generated_auth_metadata(path,os.environ["ANTHROPIC_API_KEY"])
    turn.guard(IMAGE,turn.MODEL_BASE)
    entry["request_count"]=2;target.write_text(json.dumps(data))
    turn.guard(IMAGE,turn.MODEL_BASE)
    assert os.environ["ANTHROPIC_API_KEY"] not in target.read_text()


@pytest.mark.parametrize("case",["secret","refresh","oauth","fingerprint","provider","second_entry","other_pool","source","proxy","nested","updated_secret","updated_invalid_date","unknown_top_field"])
def test_pool_exception_rejects_secrets_or_unrelated_metadata(scope,generated_pool,case):
    path,target,data,entry=generated_pool
    if case=="secret":entry["access_token"]="synthetic-secret"
    if case=="refresh":entry["refresh_token"]="synthetic-refresh"
    if case=="oauth":entry["auth_type"]="oauth"
    if case=="fingerprint":entry["secret_fingerprint"]="sha256:"+"0"*16
    if case=="provider":data["providers"]={"anthropic":{"access_token":"synthetic-secret"}}
    if case=="second_entry":data["credential_pool"]["anthropic"].append(dict(entry))
    if case=="other_pool":data["credential_pool"]["openai"]=[dict(entry)]
    if case=="source":entry["source"]="manual"
    if case=="proxy":entry["base_url"]="https://api.anthropic.com"
    if case=="nested":entry["last_error_message"]={"access_token":"synthetic-secret"}
    if case=="updated_secret":data["updated_at"]="synthetic-secret"
    if case=="updated_invalid_date":data["updated_at"]="2026-99-22T16:50:00+00:00"
    if case=="unknown_top_field":data["access_token"]="synthetic-secret"
    target.write_text(json.dumps(data))
    with pytest.raises(turn.CanaryError,match="owner_credentials"):turn.guard(IMAGE,turn.MODEL_BASE)


def test_env_metadata_exception_requires_sealed_tmpfs_symlink(scope,generated_pool):
    path,target,data,entry=generated_pool
    path.unlink();path.write_text(json.dumps(data))
    with pytest.raises(turn.CanaryError,match="owner_credentials"):turn.guard(IMAGE,turn.MODEL_BASE)


def test_real_broker_flush_records_receipt_without_delivery(scope,sink):
    base,journal=sink
    actual=RuntimeConfig.from_env();store=Store(actual.database)
    store.enqueue(outbound())
    assert Broker(actual).flush(store)
    with store.db() as db:
        row=db.execute("SELECT state,operation_id FROM outbox").fetchone()
    assert row[0]=="accepted" and row[1]==journal.snapshot()[0][2]
    assert journal.snapshot()[0][4:]==(1,1)
    body=journal.path.read_bytes()
    assert turn.ANSWER.encode() not in body and scope.egress_token.encode() not in body


def test_sink_deduplicates_and_rejects_conflicting_response_key(scope,sink):
    base,journal=sink
    first=call(base,"/v1/outbound",scope.egress_token,outbound())
    assert first[0]==200 and call(base,"/v1/outbound",scope.egress_token,outbound())==first
    assert len(journal.snapshot())==1 and journal.snapshot()[0][-1]==2
    assert call(base,"/v1/outbound",scope.egress_token,outbound(text="changed"))[0]==422
    assert journal.snapshot()[0][-1]==2


@pytest.mark.parametrize("changes",[{"conversation_id":"different"},{"cause_event_id":"other"},
    {"response_key":"scheduled:other"},{"media":{"private":"payload"}},
    {"reply_to":"someone-else"},{"text":"x"*4001}])
def test_sink_rejects_every_other_scope_and_delivery(scope,sink,changes):
    base,journal=sink
    assert call(base,"/v1/outbound",scope.egress_token,outbound(**changes))[0]==422
    assert journal.snapshot()==[]


def test_token_separation_heartbeats_empty_approvals_and_unknown_routes(scope,sink):
    base,journal=sink
    heartbeat={"ready":True,"model_ready":True,"deployment_generation":1}
    for token in (scope.ingress_token,scope.control_token,"invalid"):
        status,body=call(base,"/v1/outbound",token,outbound())
        assert status==401 and token not in json.dumps(body)
    assert call(base,"/v1/approvals",scope.egress_token)==(200,{"approvals":[]})
    path="/v1/runtime/"+turn.RUNTIME+"/heartbeat"
    assert call(base,path,scope.egress_token,heartbeat)[0]==401
    assert call(base,path,scope.control_token,heartbeat)[0]==200
    assert call(base,path,scope.control_token,{**heartbeat,"deployment_generation":2})[0]==422
    assert call(base,"/v1/send",scope.egress_token,outbound())[0]==404
    assert call(base,"/v1/media/private",scope.egress_token)[0]==404
    assert journal.snapshot()==[]


def test_non_loopback_client_url_and_symlinked_journal_are_rejected(tmp_path):
    with pytest.raises(turn.CanaryError,match="nonlocal"):turn.request("https://production.invalid/events","synthetic")
    target=tmp_path/"outside";target.touch();link=tmp_path/"journal";link.symlink_to(target)
    with pytest.raises(turn.CanaryError,match="symlink"):turn.SinkJournal(link)
    assert target.read_bytes()==b""


def test_driver_real_shim_store_and_broker_one_synthetic_turn_and_duplicate(scope,sink,monkeypatch):
    base,journal=sink;actual=RuntimeConfig.from_env();store=Store(actual.database)
    store.mark_runtime(actual.generation,True,True)
    class Shim(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def _respond(self,status,payload):
            self.send_response(status);self.end_headers();self.wfile.write(turn.encoded(payload))
        def do_POST(self):handle(self,self.path)
    server=HTTPServer(("127.0.0.1",0),Shim)
    server_thread=threading.Thread(target=server.serve_forever,daemon=True);server_thread.start()
    original=turn.request
    def request(url,token,payload=None):
        if url=="http://127.0.0.1:8080/events":return call("http://127.0.0.1:"+str(server.server_port),"/events",token,payload)
        return original(url,token,payload)
    monkeypatch.setattr(turn,"request",request)
    stop=threading.Event();model_calls=[]
    def synthetic_handler():
        while not stop.wait(.02):
            message=store.claim()
            if message is None:continue
            model_calls.append(message["event_id"])
            store.enqueue(outbound());store.finish(message["event_id"],True)
            Broker(actual).flush(store)
    worker=threading.Thread(target=synthetic_handler,daemon=True);worker.start()
    try:
        proof=turn.run_turn(journal,5,1)
        assert proof["passed"] and proof["replay_unchanged"]
        assert proof["provider_usage_verified"] is False and proof["whatsapp_delivery_verified"] is False
        assert model_calls==[turn.EVENT_ID]
        assert proof["unique_sink_receipts"]==1
        assert proof["image_ref"]==IMAGE and proof["deployment_generation"]==1
        output=json.dumps(proof)
        assert turn.ANSWER not in output and scope.ingress_token not in output and scope.egress_token not in output
        with pytest.raises(turn.CanaryError,match="existing_sink_proof"):turn.run_turn(journal,5,1)
    finally:
        stop.set();worker.join(5);server.shutdown();server.server_close();server_thread.join(5)


def test_failed_or_tool_using_turn_cannot_pass_proof(scope,tmp_path):
    store=Store(scope.database);store.mark_runtime(scope.generation,True,True)
    store.accept([turn.event()],scope);store.claim();store.finish(turn.EVENT_ID,False)
    journal=turn.SinkJournal(tmp_path/"sink.sqlite3")
    with pytest.raises(turn.CanaryError,match="gateway_turn_failed"):turn.completed(turn.runtime_snapshot(),journal)
    snapshot=turn.runtime_snapshot();snapshot["tools"]=1
    with pytest.raises(turn.CanaryError,match="tool_execution"):turn.completed(snapshot,journal)
