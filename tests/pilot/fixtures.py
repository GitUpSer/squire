"""Loopback-only HTTP fixtures, never a production provider/runtime implementation.

The runtime simulator persists accepted event IDs in SQLite and emits a bounded
fake-model reply using the real scoped messaging API. Replace /events with the
real tenant adapter to extend this test to Hermes execution.
"""
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
import httpx
from fastapi import FastAPI, HTTPException, Request

if os.environ.get("SQUIRE_PILOT_FIXTURE") != "1":
    raise RuntimeError("HTTP fixtures require explicit test mode")

CONTROL = "http://127.0.0.1:18741"
MESSAGING = "http://127.0.0.1:18742"
LOCK = threading.RLock()
STATE = {"emails": [], "sends": [], "services": {}, "volumes": {}, "domains": {},
         "teams": {}, "keys": {}, "buckets": {}, "inbound_count": 0, "sequence": 0, "events": [], "railway_operations": []}
JOURNAL = Path(os.environ["PILOT_FIXTURE_DIR"]) / "runtime-acceptance.sqlite"


def beat_loop(stop):
    with httpx.Client(timeout=3, trust_env=False) as client:
        while not stop.wait(.25):
            with LOCK:
                services = [(sid, dict(value["variables"])) for sid, value in STATE["services"].items()
                            if value.get("deployed") and not value.get("stopped")]
            for sid, variables in services:
                rid = variables.get("SQUIRE_RUNTIME_ID")
                if not rid:
                    continue
                try:
                    client.post(CONTROL + f"/v1/runtime/{rid}/heartbeat",
                        headers={"Authorization": "Bearer " + variables["SQUIRE_CONTROL_TOKEN"]},
                        json={"ready": True, "model_ready": bool(variables.get("ANTHROPIC_API_KEY")),
                              "deployment_generation": int(variables["SQUIRE_DEPLOYMENT_GENERATION"])})
                except httpx.HTTPError:
                    pass


@asynccontextmanager
async def lifespan(app):
    with sqlite3.connect(JOURNAL) as db:
        db.execute("CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, runtime TEXT, payload TEXT, replied INTEGER DEFAULT 0)")
    stop = threading.Event()
    worker = threading.Thread(target=beat_loop, args=(stop,), daemon=True)
    worker.start()
    yield
    stop.set()
    worker.join(5)


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
def health():
    return {"status": "fixture-only"}


@app.post("/emails")
async def email(request: Request):
    data = await request.json()
    with LOCK:
        STATE["emails"].append(data)
    return {"id": "email-" + str(len(STATE["emails"]))}


@app.post("/graphql")
async def railway(request: Request):
    data = await request.json()
    query, variables = data["query"], data.get("variables", {})
    op = re.sub(r"^(query|mutation)\s+", "", query.strip()).split("(")[0].split("{")[0].strip()
    with LOCK:
        STATE["railway_operations"].append(op)
        if op == "pilotBuckets":
            result={"project":{"buckets":{"edges":[{"node":{"id":bid,"name":bucket["name"],"deletedAt":None}} for bid,bucket in STATE["buckets"].items()],"pageInfo":{"hasNextPage":False,"endCursor":None}}}}
        elif op == "pilotBucketCreate":
            values=variables["input"]
            if values["projectId"]!="fixture-project" or "environmentId" in values:raise HTTPException(422,"bucket fixture requires project identity only")
            existing=next((bid for bid,bucket in STATE["buckets"].items() if bucket["name"]==values["name"]),None)
            bid=existing or "fixture-bucket-"+str(len(STATE["buckets"])+1)
            STATE["buckets"][bid]={"name":values["name"]}
            result={"bucketCreate":{"id":bid,"name":values["name"]}}
        elif op == "project":
            if "volumes" in query:
                result = {"project": {"volumes": {"edges": [{"node": {"id": vid,
                    "volumeInstances": {"edges": [{"node": {"id": "vi-"+vid,"serviceId": sid}}]}}}
                    for vid, sid in STATE["volumes"].items()]}}}
            else:
                result = {"project": {"services": {"edges": [{"node": {"id": sid,"name": service["name"]}}
                    for sid, service in STATE["services"].items()]}}}
        elif op == "serviceCreate":
            sid = "fixture-service-" + str(len(STATE["services"])+1)
            name = variables["input"]["name"]
            STATE["services"][sid] = {"name": name,"variables": {},"deployed": False,"image":variables["input"].get("source",{}).get("image"),"registry_configured":bool(variables["input"].get("registryCredentials"))}
            result = {op: {"id": sid,"name": name}}
        elif op == "volumeCreate":
            vid = "fixture-volume-"+str(len(STATE["volumes"])+1)
            STATE["volumes"][vid] = variables["input"]["serviceId"]
            result = {op: {"id": vid}}
        elif op == "variables":
            result = {op: {key: "redacted" for key in STATE["services"][variables["serviceId"]]["variables"]}}
        elif op == "variableCollectionUpsert":
            values = variables["input"]
            STATE["services"][values["serviceId"]]["variables"].update(values["variables"])
            result = {op: True}
        elif op == "serviceInstanceDeploy":
            sid = variables.get("serviceId") or variables.get("input", {}).get("serviceId")
            STATE["services"][sid]["deployed"] = True
            STATE["services"][sid]["stopped"] = False
            result = {op: True}
        elif op == "serviceInstanceUpdate":
            values=variables["input"];service=STATE["services"][variables["serviceId"]]
            if "source" in values:service["image"]=values["source"].get("image")
            if "registryCredentials" in values:service["registry_configured"]=bool(values["registryCredentials"])
            result = {op: True}
        elif op == "deployments":
            result = {op: {"edges": [{"node": {"id": "fixture-deploy", "status": "SUCCESS"}}]}}
        elif op == "domains":
            domain = STATE["domains"].get(variables["serviceId"])
            result = {op: {"serviceDomains": [{"domain": domain}] if domain else []}}
        elif op == "serviceDomainCreate":
            sid = variables["input"]["serviceId"]
            domain = STATE["domains"].setdefault(sid, sid+".pilot.test")
            result = {op: {"id": "domain-"+sid,"domain": domain}}
        elif op == "volumeDelete":
            STATE["volumes"].pop(variables["volumeId"], None)
            result = {op: True}
        elif op == "serviceDelete":
            STATE["services"].pop(variables["id"], None)
            result = {op: True}
        elif op == "deploymentStop":
            result = {op: True}
        else:
            raise HTTPException(501, "unimplemented fixture operation: "+op)
    return {"data": result}


@app.get("/team/info")
def team_info(team_id: str):
    team = STATE["teams"].get(team_id)
    if not team:
        raise HTTPException(404, "fixture team absent")
    return {"team_info": team}


@app.post("/team/new")
async def team_new(request: Request):
    team = await request.json()
    STATE["teams"][team["team_id"]] = team
    return team


@app.post("/team/update")
async def team_update(request: Request):
    data=await request.json()
    if data["team_id"] not in STATE["teams"]:raise HTTPException(404,"fixture team absent")
    if set(data)-{"team_id","max_budget"}:raise HTTPException(422,"budget update may not reset spend")
    STATE["teams"][data["team_id"]].update(data)
    return STATE["teams"][data["team_id"]]


@app.post("/key/delete")
async def key_delete(request: Request):
    data = await request.json()
    for alias in data.get("key_aliases", []):
        STATE["keys"].pop(alias, None)
    return {"deleted_keys": data.get("key_aliases", [])}


@app.post("/key/generate")
async def key_generate(request: Request):
    data = await request.json()
    STATE["keys"][data["key_alias"]] = data
    return {"key": "fixture-model-key-"+data["key_alias"]}


@app.post("/v1/send")
async def provider_send(request: Request):
    if request.headers.get("Authorization") != "Bearer " + "p"*40:
        raise HTTPException(401)
    data = await request.json()
    with LOCK:
        existing = next((r for r in STATE["sends"] if r["operation_id"] == data["operation_id"]), None)
        if existing:
            if existing["text"] != data["text"] or existing["recipient"] != data["recipient"]:
                raise HTTPException(409)
            return existing["result"]
        result = {"operation_id": data["operation_id"],"status":"accepted",
                  "provider_message_id":"fixture-send-"+str(len(STATE["sends"])+1)}
        STATE["sends"].append({**data,"result":result,"sent_at":time.time()})
    return result


@app.get("/v1/groups/{jid}")
def group_metadata(jid: str):
    return {"jid": jid,"fetched_at":time.time(),"participants":[
        {"jid":"15550000101@s.whatsapp.net","admin":False},
        {"jid":"999999@lid","phone_jid":"15550000199@s.whatsapp.net","admin":True}]}


@app.post("/events")
async def runtime_events(request: Request):
    payload = await request.json()
    authorization = request.headers.get("Authorization")
    with LOCK:
        candidates = [dict(s["variables"]) for s in STATE["services"].values()
                      if "Bearer "+s["variables"].get("SQUIRE_INGRESS_TOKEN", "") == authorization]
    if len(candidates) != 1:
        raise HTTPException(401)
    variables = candidates[0]
    for event in payload.get("events", []):
        if event["runtime_id"] != variables["SQUIRE_RUNTIME_ID"] or event["conversation_id"] != variables["SQUIRE_CONVERSATION_ID"]:
            raise HTTPException(403)
        with sqlite3.connect(JOURNAL) as db:
            db.execute("INSERT OR IGNORE INTO events(id,runtime,payload) VALUES(?,?,?)",
                (event["event_id"],event["runtime_id"],json.dumps(event)))
            row = db.execute("SELECT replied FROM events WHERE id=?",(event["event_id"],)).fetchone()
        if not row[0]:
            with httpx.Client(timeout=10, trust_env=False) as client:
                result = client.post(MESSAGING+"/v1/outbound",
                    headers={"Authorization":"Bearer "+variables["SQUIRE_EGRESS_TOKEN"]},
                    json={"conversation_id":event["conversation_id"],"response_key":"fixture-response:"+event["event_id"],
                          "cause_event_id":event["event_id"],"text":"Fixture model reply: "+event["payload"]["text"]})
                if result.status_code != 200:
                    raise HTTPException(503,"scoped egress rejected fixture reply")
            with sqlite3.connect(JOURNAL) as db:
                db.execute("UPDATE events SET replied=1 WHERE id=?",(event["event_id"],))
            with LOCK:
                STATE["events"].append(event)
    return {"accepted": True}


@app.get("/fixture/status")
def fixture_status():
    with LOCK:
        return STATE


@app.post("/fixture/inbound")
async def inject(request: Request):
    command = await request.json()
    with LOCK:
        if command.get("replay"):
            batch = STATE["last_batch"]
        else:
            before = str(STATE["sequence"]) if STATE["sequence"] else None
            STATE["sequence"] += 1
            seq = STATE["sequence"]
            batch = {"provider_account_id":"squire","store_id":"fixture-live",
                     "checkpoint_before":before,"checkpoint_after":str(seq),"events":[{
                         "event_id":f"fixture-incoming-{seq}","sequence":seq,
                         "provider_message_id":f"fixture-provider-{seq}","chat_jid":command["chat_jid"],
                         "sender_jid":command.get("sender_jid",command["chat_jid"]),
                         "chat_type":command.get("chat_type","dm"),"origin":"live",
                         "timestamp":time.time(),"expires_at":None,"from_me":False,"text":command["text"]}]}
            STATE["last_batch"] = batch
            STATE["inbound_count"] += 1
    with httpx.Client(timeout=15, trust_env=False) as client:
        result = client.post(MESSAGING+"/v1/ingest/batches",json=batch,headers={"Authorization":"Bearer "+"t"*40})
        if result.status_code != 200:
            raise HTTPException(result.status_code, result.text)
        return result.json()
