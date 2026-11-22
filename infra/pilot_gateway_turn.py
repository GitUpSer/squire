#!/usr/bin/env python3
"""One real gateway turn in the named standard canary; no WhatsApp transport.

Operator only. `serve` binds a finite-lived loopback broker sink. `run` submits
one fixed synthetic event, observes the actual runtime ledger, then replays that
identical event to check deduplication. Running it can incur model usage; supply
only a short-lived, budgeted proxy virtual key and revoke that key afterward.
Proofs contain counts/hashes, never credentials or model/message contents.
The driver deadline does not cancel an executing model turn. On any failure,
revoke the key and stop the gateway before inspecting or reconciling its ledger.
Keep the proof/sink journal; cleanup may remove only this exact completed inbox
event and accepted outbox response after key revocation and gateway stop.
"""
import argparse
from contextlib import closing, contextmanager
from datetime import datetime
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

SERVICE = "2dc2ff7e-aa9e-4b1f-b028-673a90702b66"
RUNTIME = "pilot-canary-standard-2433fe3cb470"
CONVERSATION = "canary-f51c207f0427"
HOME = Path("/opt/data")
SINK = "http://127.0.0.1:18777"
MODEL_BASE = "http://trial-proxy.railway.internal:4000"
EVENT_ID = "squire-standard-real-gateway-turn-v1"
RESPONSE_KEY = "event:"+EVENT_ID+":0"
ANSWER = "SQUIRE_CANARY_OK"


class CanaryError(RuntimeError):
    pass


def require(ok, reason):
    if not ok:
        raise CanaryError(reason)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def event():
    return {"event_id": EVENT_ID, "conversation_id": CONVERSATION,
            "tenant_id": RUNTIME, "runtime_id": RUNTIME, "chat_type": "dm",
            "sender_id": "synthetic-canary", "provider_message_id": EVENT_ID,
            "timestamp": 1000000000.0,
            "payload": {"text": "This is a synthetic connectivity check. Reply with exactly SQUIRE_CANARY_OK and nothing else. Do not use any tools.","expires_at":None}}


def safe_file(path):
    for part in (path, *path.parents):
        require(not part.is_symlink(), "unexpected_proof_or_database_symlink")
    return path


def generated_auth_metadata(path,key):
    """Allow only Hermes' sealed, secret-free reference to this virtual env key.

    Pinned agent/credential_persistence.py uses sha256:<first 16 hex> for
    borrowed sources; it deliberately removes their access_token before saving.
    """
    try:
        if not key or not path.is_symlink() or not path.resolve(strict=True).is_relative_to(Path("/dev/shm")):
            return False
        data=json.loads(path.read_text())
        required={"version","providers","credential_pool"}
        if not isinstance(data,dict) or not required<=set(data) or set(data)-required-{"updated_at"} or type(data["version"]) is not int or data["version"]!=1 or data["providers"]!={}:
            return False
        if "updated_at" in data:
            # hermes_cli/auth.py stamps datetime.now(timezone.utc).isoformat().
            updated=data["updated_at"]
            if not isinstance(updated,str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?\+00:00",updated):return False
            datetime.fromisoformat(updated)
        pools=data["credential_pool"]
        if not isinstance(pools,dict) or set(pools)!={"anthropic"} or not isinstance(pools["anthropic"],list) or len(pools["anthropic"])!=1:
            return False
        entry=pools["anthropic"][0]
        allowed={"id","label","auth_type","priority","source","last_status","last_status_at","last_error_code","last_error_reason","last_error_message","last_error_reset_at","base_url","request_count","secret_fingerprint"}
        if not isinstance(entry,dict) or set(entry)-allowed or any(isinstance(v,(dict,list)) for v in entry.values()):
            return False
        expected={"label":"ANTHROPIC_API_KEY","source":"env:ANTHROPIC_API_KEY","auth_type":"api_key","base_url":MODEL_BASE}
        if any(entry.get(k)!=v for k,v in expected.items()):return False
        if not isinstance(entry.get("id"),str) or not 1<=len(entry["id"])<=64 or type(entry.get("priority")) is not int or type(entry.get("request_count")) is not int or entry["request_count"]<0:
            return False
        fingerprint="sha256:"+hashlib.sha256(key.encode("utf-8",errors="surrogatepass")).hexdigest()[:16]
        return isinstance(entry.get("secret_fingerprint"),str) and hmac.compare_digest(entry["secret_fingerprint"],fingerprint)
    except (OSError,ValueError,TypeError,KeyError):
        return False


def guard(image, model_base=None):
    values = {"RAILWAY_SERVICE_ID": SERVICE, "SQUIRE_RUNTIME_ID": RUNTIME,
              "SQUIRE_OWNER_TENANT_ID": RUNTIME, "SQUIRE_CONVERSATION_ID": CONVERSATION,
              "SQUIRE_RUNTIME_KIND": "dm", "SQUIRE_HINDSIGHT_VARIANT": "standard",
              "HOME": str(HOME), "HERMES_HOME": str(HOME), "SQUIRE_VOLUME": str(HOME),
              "SQUIRE_MESSAGING_URL": SINK, "HINDSIGHT_API_WORKER_ENABLED": "false",
              "HINDSIGHT_API_LLM_PROVIDER": "none"}
    require(all(os.environ.get(k) == v for k,v in values.items()), "canary_scope_or_isolation_mismatch")
    require(os.environ.get("CONTROL_API_URL") in {SINK,"http://127.0.0.1:9"}, "control_endpoint_not_isolated")
    require(os.environ.get("SQUIRE_RESTORED_HOME") != "true", "founder_state_forbidden")
    require(re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}",image or "") and os.environ.get("SQUIRE_IMAGE_REF") == image, "immutable_image_mismatch")
    require(str(HOME) in {line.split()[4] for line in Path("/proc/self/mountinfo").read_text().splitlines()}, "canary_volume_not_mounted")
    tokens = [os.environ.get(k,"") for k in ("SQUIRE_INGRESS_TOKEN","SQUIRE_EGRESS_TOKEN","SQUIRE_CONTROL_TOKEN")]
    require(all(len(value)>=32 for value in tokens) and len(set(tokens))==3, "invalid_scoped_capabilities")
    auth = HOME/"auth.json"
    require((not auth.exists() and not auth.is_symlink()) or (auth.is_file() and auth.stat().st_size==0)
            or generated_auth_metadata(auth,os.environ.get("ANTHROPIC_API_KEY","")), "owner_credentials_forbidden")
    forbidden = ("OPENAI_API_KEY","OPENROUTER_API_KEY","ANTHROPIC_OAUTH_TOKEN","RAILWAY_API_TOKEN","GH_TOKEN","GITHUB_TOKEN","TELEGRAM_BOT_TOKEN","WHATSAPP_ACCESS_TOKEN")
    require(not any(os.environ.get(key) for key in forbidden), "unrelated_credentials_forbidden")
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    require(not key or (key.startswith("sk-") and not key.startswith("sk-ant-")), "use_proxy_virtual_key_only")
    require(not key or os.environ.get("ANTHROPIC_BASE_URL", "").rstrip("/")==MODEL_BASE,"virtual_key_must_use_private_trial_proxy")
    if model_base is not None:
        require(bool(key) and model_base.rstrip("/")==MODEL_BASE, "proxy_model_configuration_mismatch")
    profile = json.loads((HOME/".squire/runtime-profile.json").read_text())
    require((profile.get("runtime_id"),profile.get("conversation_id"),profile.get("kind")) == (RUNTIME,CONVERSATION,"dm"), "volume_profile_mismatch")


class SinkJournal:
    def __init__(self, path):
        self.path = safe_file(Path(path))
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd=os.open(self.path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600);os.close(fd)
        self.path.chmod(0o600)
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS receipts(response_key TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,operation_id TEXT NOT NULL,reply_sha256 TEXT NOT NULL,reply_matches INTEGER NOT NULL,attempts INTEGER NOT NULL)")

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.path,timeout=5)) as db:
            with db:yield db

    def accept(self, payload):
        require(isinstance(payload,dict) and set(payload) <= {"conversation_id","response_key","cause_event_id","text","priority","reply_to"}, "outbound_shape_rejected")
        require(payload.get("conversation_id")==CONVERSATION and payload.get("cause_event_id")==EVENT_ID and payload.get("response_key")==RESPONSE_KEY, "outbound_scope_rejected")
        require(payload.get("priority")=="interactive" and payload.get("reply_to") in {None,EVENT_ID}, "outbound_delivery_rejected")
        require(isinstance(payload.get("text"),str) and 0<len(payload["text"])<=4000, "outbound_text_rejected")
        fingerprint=digest(encoded(payload));operation="canary-"+digest(RESPONSE_KEY.encode())
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            previous=db.execute("SELECT fingerprint FROM receipts WHERE response_key=?",(RESPONSE_KEY,)).fetchone()
            require(previous is None or previous[0]==fingerprint,"outbound_identity_conflict")
            if previous:
                db.execute("UPDATE receipts SET attempts=attempts+1 WHERE response_key=?",(RESPONSE_KEY,))
            else:
                db.execute("INSERT INTO receipts VALUES(?,?,?,?,?,1)",(RESPONSE_KEY,fingerprint,operation,digest(payload["text"].encode()),int(payload["text"].strip()==ANSWER)))
        return {"operation_id":operation}

    def snapshot(self):
        with self.db() as db:
            rows=db.execute("SELECT response_key,fingerprint,operation_id,reply_sha256,reply_matches,attempts FROM receipts").fetchall()
        return rows


def handler(journal, egress_token, control_token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass

        def setup(self):
            super().setup();self.connection.settimeout(3)

        def respond(self,status,payload):
            body=encoded(payload)
            self.send_response(status);self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)

        def authorized(self,token):
            if hmac.compare_digest(self.headers.get("Authorization",""),"Bearer "+token): return True
            self.close_connection=True;self.respond(401,{"error":"unauthorized"});return False

        def do_GET(self):
            if self.path=="/v1/approvals":
                if self.authorized(egress_token): self.respond(200,{"approvals":[]})
            elif self.path=="/__canary/status":
                if self.authorized(control_token): self.respond(200,{"fixture":"squire-single-turn-v1","receipts":len(journal.snapshot())})
            else: self.respond(404,{"error":"unknown_route"})

        def do_POST(self):
            heartbeat=self.path=="/v1/runtime/"+RUNTIME+"/heartbeat"
            if not heartbeat and self.path!="/v1/outbound":
                self.close_connection=True;self.respond(404,{"error":"unknown_route"});return
            if not self.authorized(control_token if heartbeat else egress_token):return
            try:
                require(not self.headers.get("Transfer-Encoding"),"unsupported_framing")
                length=int(self.headers.get("Content-Length","0"));require(0<length<=16384,"invalid_body_length")
                payload=json.loads(self.rfile.read(length))
                if heartbeat:
                    require(isinstance(payload,dict) and set(payload)=={"ready","model_ready","deployment_generation"},"heartbeat_shape_rejected")
                    require(type(payload["ready"]) is bool and type(payload["model_ready"]) is bool and type(payload["deployment_generation"]) is int and payload["deployment_generation"]==int(os.environ["SQUIRE_DEPLOYMENT_GENERATION"]),"heartbeat_scope_rejected")
                    result={"accepted":True}
                else: result=journal.accept(payload)
                self.respond(200,result)
            except (ValueError,TypeError,CanaryError,KeyError):
                self.close_connection=True;self.respond(422,{"error":"invalid_canary_request"})
    return Handler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None


def request(url,token,payload=None):
    require(url.startswith(SINK+"/") or url=="http://127.0.0.1:8080/events", "nonlocal_fixture_request_forbidden")
    req=Request(url,data=encoded(payload) if payload is not None else None,
                headers={"Authorization":"Bearer "+token,"Content-Type":"application/json"})
    with build_opener(ProxyHandler({}),NoRedirect()).open(req,timeout=5) as response:
        return response.status,json.loads(response.read(16384))


def runtime_snapshot():
    path=safe_file(HOME/".squire/messaging.sqlite3")
    with closing(sqlite3.connect(path.as_uri()+"?mode=ro",uri=True)) as db:
        row=db.execute("SELECT pid,generation,ready,model_ready,updated FROM runtime WHERE id=1").fetchone()
        require(row and row[1]==int(os.environ["SQUIRE_DEPLOYMENT_GENERATION"]) and row[2:4]==(1,1) and time.time()-row[4]<30 and Path(f"/proc/{row[0]}").exists(), "actual_adapter_not_model_ready")
        inbox=db.execute("SELECT event_id,fingerprint,state,started,finished,failure FROM inbox").fetchall()
        outbox=db.execute("SELECT response_key,payload,state,operation_id FROM outbox").fetchall()
        tools=db.execute("SELECT count(*) FROM tools").fetchone()[0]
    return {"inbox":inbox,"outbox":outbox,"tools":tools}


def completed(snapshot,journal):
    require(snapshot["tools"]==0,"unexpected_tool_execution")
    require(len(snapshot["inbox"])==1 and snapshot["inbox"][0][0]==EVENT_ID and snapshot["inbox"][0][1]==digest(encoded(event())),"unexpected_inbox_scope")
    inbox=snapshot["inbox"][0]
    require(inbox[2] not in {"uncertain","cancelled","reconciled"},"gateway_turn_failed")
    receipts=journal.snapshot()
    require(len(snapshot["outbox"])<=1 and len(receipts)<=1,"unexpected_outbound_activity")
    if inbox[2]!="done" or not snapshot["outbox"] or not receipts:return False
    outbox=snapshot["outbox"][0];receipt=receipts[0]
    require(inbox[3] is not None and inbox[4] is not None and inbox[5] is None,"incomplete_gateway_execution")
    require(outbox[0]==receipt[0]==RESPONSE_KEY and digest(encoded(json.loads(outbox[1])))==receipt[1],"sink_ledger_mismatch")
    require(receipt[4]==1,"unexpected_model_reply")
    if outbox[2]=="pending":return False
    require(outbox[2]=="accepted" and outbox[3]==receipt[2],"egress_not_accepted")
    return True


def run_turn(journal,timeout,observe):
    require(1<=timeout<=180 and 1<=observe<=30,"bounded_timeout_required")
    require(not journal.snapshot(),"existing_sink_proof_requires_review")
    before=runtime_snapshot()
    require(not before["inbox"] and not before["outbox"] and before["tools"]==0,"runtime_not_empty")
    status,body=request(SINK+"/__canary/status",os.environ["SQUIRE_CONTROL_TOKEN"])
    require(status==200 and body=={"fixture":"squire-single-turn-v1","receipts":0},"sink_not_ready")
    envelope={"events":[event()]}
    def submit():
        status,body=request("http://127.0.0.1:8080/events",os.environ["SQUIRE_INGRESS_TOKEN"],envelope)
        require(status==202 and body=={"accepted":True,"event_ids":[EVENT_ID]},"runtime_did_not_acknowledge")
    submit();deadline=time.monotonic()+timeout
    while True:
        snapshot=runtime_snapshot()
        if completed(snapshot,journal):break
        require(time.monotonic()<deadline,"gateway_turn_deadline_exceeded")
        time.sleep(.25)
    receipt_before=journal.snapshot()
    submit();deadline=time.monotonic()+observe
    while time.monotonic()<deadline:
        time.sleep(.25)
        require(runtime_snapshot()==snapshot and journal.snapshot()==receipt_before,"duplicate_event_reexecuted")
    return {"format":"squire-gateway-turn-proof-v1","passed":True,"event_sha256":digest(encoded(event())),
            "image_ref":os.environ["SQUIRE_IMAGE_REF"],"deployment_generation":int(os.environ["SQUIRE_DEPLOYMENT_GENERATION"]),
            "railway_deployment_id":os.environ.get("RAILWAY_DEPLOYMENT_ID"),"model_base_sha256":digest(MODEL_BASE.encode()),
            "runtime_id_sha256":digest(RUNTIME.encode()),"inbox_done":1,"outbox_accepted":1,"tools":0,
            "unique_sink_receipts":1,"reply_sha256":receipt_before[0][3],"exact_reply_matches":True,
            "replay_unchanged":True,"replay_observation_seconds":observe,
            "provider_usage_verified":False,"whatsapp_delivery_verified":False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=("serve","run"))
    parser.add_argument("--expected-image",required=True)
    parser.add_argument("--expected-model-base")
    parser.add_argument("--lifetime-seconds",type=int,default=600)
    parser.add_argument("--timeout-seconds",type=int,default=120)
    parser.add_argument("--replay-observe-seconds",type=int,default=5)
    args=parser.parse_args()
    require(args.command!="run" or args.expected_model_base,"expected_proxy_base_required")
    guard(args.expected_image,args.expected_model_base if args.command=="run" else None)
    destination=safe_file(HOME/".squire/canary-turn-proof.json")
    require(args.command!="run" or not destination.exists(),"existing_turn_proof_requires_review")
    journal=SinkJournal(HOME/".squire/canary-turn-sink.sqlite3")
    if args.command=="serve":
        require(1<=args.lifetime_seconds<=900,"bounded_sink_lifetime_required")
        with HTTPServer(("127.0.0.1",18777),handler(journal,os.environ["SQUIRE_EGRESS_TOKEN"],os.environ["SQUIRE_CONTROL_TOKEN"])) as server:
            server.timeout=.5;deadline=time.monotonic()+args.lifetime_seconds
            print(json.dumps({"fixture":"squire-single-turn-v1","listening":True,"lifetime_seconds":args.lifetime_seconds}),flush=True)
            while time.monotonic()<deadline:server.handle_request()
    else:
        proof=run_turn(journal,args.timeout_seconds,args.replay_observe_seconds)
        fd=os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,"w") as stream:json.dump(proof,stream,sort_keys=True);stream.flush();os.fsync(stream.fileno())
        print(json.dumps(proof,sort_keys=True))


if __name__=="__main__":
    try:main()
    except Exception as exc:
        print(json.dumps({"passed":False,"error":str(exc) if isinstance(exc,CanaryError) else type(exc).__name__}))
        raise SystemExit(1) from None
