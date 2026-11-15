from concurrent.futures import ThreadPoolExecutor
import re
import time
import httpx
import pytest
from fastapi import HTTPException
from sqlmodel import Session, select
from messaging.config import settings
from messaging.crypto import open_sealed
from messaging.db import engine
from messaging.groups import handle_command, participates
from messaging.ingest import accept_batch
from messaging.models import (Binding, Checkpoint, ControlCommand, InboxEvent, Observation,
    Outbox, PhoneChallenge, ProviderAccount, Runtime, TenantState)
from messaging.schemas import (ChallengeRequest, Event, GroupSnapshot, ImportBinding,
    IngestBatch, OutboundRequest, RuntimeRegistration)
from messaging.service import (create_group_claim, enqueue_runtime, import_binding,
    lock_tenant, now, queue_text, register_runtime)
from messaging.worker import (claim_inbound, claim_outbound, dispatch, finish_inbound,
    recover_leases, send)
from conftest import headers


def imported(tenant="owner", phone="15550000001@s.whatsapp.net", kind="dm", owner=None):
    with Session(engine()) as db:
        result = import_binding(db, ImportBinding(operation_id="import-"+tenant+kind,
            tenant_id=tenant, account_id="account-"+tenant, recipient=phone, kind=kind,
            owner_recipient=owner, activated_at=now()-60, group_limit=3))
        db.commit()
        return result["conversation_id"]


def runtime(binding, tenant="owner", kind="dm", token="d"*40):
    with Session(engine()) as db:
        register_runtime(db, RuntimeRegistration(runtime_id=tenant+kind, tenant_id=tenant,
            conversation_id=binding, kind=kind, endpoint=f"http://{tenant}{kind}.railway.internal/events",
            ingress_token="i"*40, egress_token=token))
        db.commit()
    return tenant+kind


def event(sequence=1, text="hello", chat="15550000001@s.whatsapp.net", **kwargs):
    return Event(event_id=f"event-{sequence}", sequence=sequence,
        origin=kwargs.pop("origin", "live"),
        provider_message_id=kwargs.pop("provider_message_id", f"provider-{sequence}"),
        chat_jid=chat, sender_jid=kwargs.pop("sender_jid", chat),
        timestamp=kwargs.pop("timestamp", now()), text=text, **kwargs)


def ingest(events, before=None, after="1", store="store"):
    with Session(engine()) as db:
        result = accept_batch(db, IngestBatch(provider_account_id="squire",store_id=store,
            checkpoint_before=before, checkpoint_after=after, events=events))
        db.commit()
        return result


def enable():
    with Session(engine()) as db:
        account = db.get(ProviderAccount, "squire")
        account.paused = False
        db.add(account)
        db.commit()


def test_principals_scope_encryption_and_idempotent_outbox(client):
    binding = imported()
    other = imported("other", "15550000002@s.whatsapp.net")
    runtime(binding)
    assert client.get("/v1/control/tenants/owner", headers=headers("transport")).status_code == 401
    request = {"conversation_id": binding, "response_key": "answer-1", "text": "private secret"}
    first = client.post("/v1/outbound", headers=headers("dm"), json=request)
    assert first.status_code == 200, first.text
    duplicate = client.post("/v1/outbound", headers=headers("dm"), json=request)
    assert duplicate.json() == first.json()
    request["conversation_id"] = other
    assert client.post("/v1/outbound", headers=headers("dm"), json=request).status_code == 403
    request["recipient"] = "15550000003@s.whatsapp.net"
    assert client.post("/v1/outbound", headers=headers("dm"), json=request).status_code == 422
    with Session(engine()) as db:
        row = db.get(Outbox, first.json()["operation_id"])
        assert "private secret" not in row.payload_enc
        assert "15550000001" not in db.get(Binding, binding).address_enc
        assert "d"*40 != db.get(Runtime, "ownerdm").egress_token_hash


def test_phone_challenge_failed_attempts_binding_and_no_duplicate_intro(client):
    body = {"operation_id": "phone-operation", "tenant_id": "new", "account_id": "new-account",
            "phone": "+15550000005", "consent_version": "v1"}
    response = client.post("/v1/control/phone/challenges", headers=headers("control"), json=body)
    assert response.status_code == 200, response.text
    cid = response.json()["challenge_id"]
    assert client.post("/v1/control/phone/challenges", headers=headers("control"), json=body).json() == response.json()
    with Session(engine()) as db:
        challenge = db.get(PhoneChallenge, cid)
        row = db.get(Outbox, challenge.outbox_id)
        code = re.search(r"code is (\d{8})", open_sealed(row.payload_enc, "outbox:"+row.id)["text"])[1]
    verification = {"account_id":"new-account", "challenge_id":cid, "code":"00000000" if code != "00000000" else "00000001"}
    assert client.post("/v1/control/phone/verify", headers=headers("control"), json=verification).status_code == 400
    with Session(engine()) as db:
        assert db.get(PhoneChallenge, cid).attempts == 1
    verification["code"] = code
    verified = client.post("/v1/control/phone/verify", headers=headers("control"), json=verification)
    assert verified.status_code == 200, verified.text
    binding = verified.json()["conversation_id"]
    runtime(binding, "new")
    runtime(binding, "new")
    with Session(engine()) as db:
        assert len(db.exec(select(Outbox).where(Outbox.binding_id == binding)).all()) == 1


def test_checkpoint_exact_replay_conflict_and_logical_upsert():
    imported()
    first = event()
    assert ingest([first])["events_recorded"] == 1
    assert ingest([first])["duplicate"]
    with pytest.raises(HTTPException) as error:
        ingest([event(1).model_copy(update={"event_id": "forged"})])
    assert error.value.status_code == 409
    ingest([event(2, "edited", provider_message_id=first.provider_message_id)], "1", "2")
    with Session(engine()) as db:
        rows = db.exec(select(InboxEvent)).all()
        assert len(rows) == 1
        assert open_sealed(rows[0].payload_enc, "inbox:"+rows[0].id)["text"] == "edited"
        assert len(db.exec(select(Observation)).all()) == 2
        assert db.get(Checkpoint, "store").checkpoint == "2"


def test_historical_unknown_and_from_me_never_start_turn():
    binding = imported()
    ingest([event(1, origin="history"), event(2, from_me=True),
            event(3, timestamp=now()-120), event(4, chat="15559999999@s.whatsapp.net")],after="4")
    assert claim_inbound() is None
    with Session(engine()) as db:
        assert all(r.payload_enc is None for r in db.exec(select(InboxEvent)))


def test_stop_suppresses_pending_outbound_and_queues_control_pause():
    binding = imported()
    rid = runtime(binding)
    with Session(engine()) as db:
        enqueue_runtime(db, db.get(Runtime, rid), OutboundRequest(conversation_id=binding,response_key="hello",text="hello"))
        db.commit()
    ingest([event(text="STOP")])
    with Session(engine()) as db:
        assert db.get(TenantState, "owner").opted_out
        assert all(r.state == "cancelled" for r in db.exec(select(Outbox)))
        assert db.exec(select(ControlCommand)).one().kind == "tenant_state:paused"
    assert claim_inbound() is None


def test_fallback_nonce_cannot_bind_forwarded_sender(client):
    payload = {"operation_id":"fallback-op", "tenant_id":"new","account_id":"account",
               "phone":"+15550000007","consent_version":"v1"}
    result = client.post("/v1/control/phone/challenges",headers=headers("control"),json=payload).json()
    nonce = result["fallback_nonce"]
    ingest([event(text="SQUIRE-CONNECT "+nonce, chat="15550000008@s.whatsapp.net")])
    with Session(engine()) as db:
        assert db.get(PhoneChallenge,result["challenge_id"]).consumed_at is None
    ingest([event(2,text="SQUIRE-CONNECT "+nonce,chat="15550000007@s.whatsapp.net")],"1","2")
    with Session(engine()) as db:
        assert db.get(PhoneChallenge,result["challenge_id"]).consumed_at is not None


def test_group_owner_and_current_admin_both_required():
    imported()
    with Session(engine()) as db:
        claim = create_group_claim(db, "owner", "claim-operation")
        db.commit()
    group = "120333444@g.us"
    snapshot = GroupSnapshot(jid=group,fetched_at=now(),participants=[
        {"jid":"15550000001@s.whatsapp.net","admin":False},
        {"jid":"15550000002@s.whatsapp.net","admin":True}])
    def command(text, sender):
        with Session(engine()) as db:
            result = handle_command(db,event(text=text,chat=group,chat_type="group",sender_jid=sender),snapshot)
            db.commit()
            return result
    command("!squire approve "+claim["nonce"],"15550000002@s.whatsapp.net")
    with Session(engine()) as db:
        assert not db.exec(select(Binding).where(Binding.kind=="group")).first()
    command("!squire claim "+claim["nonce"],"15550000001@s.whatsapp.net")
    command("!squire approve "+claim["nonce"],"15550000003@s.whatsapp.net")
    with Session(engine()) as db:
        assert not db.exec(select(Binding).where(Binding.kind=="group")).first()
    command("!squire approve "+claim["nonce"],"15550000002@s.whatsapp.net")
    with Session(engine()) as db:
        binding = db.exec(select(Binding).where(Binding.kind=="group")).one()
        assert not participates(db,binding,event(chat=group,chat_type="group"),snapshot)
        assert participates(db,binding,event(text="!squire hello",chat=group,chat_type="group"),snapshot)
        removed = snapshot.model_copy(update={"participants":snapshot.participants[1:]})
        assert not participates(db,binding,event(text="!squire hello",chat=group,chat_type="group"),removed)
        assert not binding.active


def test_uncertain_submission_is_terminal_and_stale_worker_cannot_send():
    binding = imported()
    runtime(binding)
    with Session(engine()) as db:
        queue_text(db,tenant_id="owner",binding=db.get(Binding,binding),response_key="one",text="hello")
        db.commit()
    enable()
    claim = claim_outbound()
    calls = []
    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("entitlement"):
            return httpx.Response(200,json={"allowed":True,"state":"imported_active","runtime_ready":True})
        raise httpx.ReadTimeout("lost response")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        send(claim, client)
        send(claim, client)
    with Session(engine()) as db:
        assert db.get(Outbox,claim.id).state == "uncertain"
    assert calls.count("/v1/send") == 1
    assert claim_outbound() is None


def test_crash_after_dispatch_intent_recovers_uncertain():
    binding = imported()
    with Session(engine()) as db:
        row = queue_text(db,tenant_id="owner",binding=db.get(Binding,binding),response_key="one",text="hello")
        row.state, row.lease_until = "dispatching", now()-1
        db.add(row)
        db.commit()
        oid = row.id
    recover_leases()
    with Session(engine()) as db:
        assert db.get(Outbox,oid).state == "uncertain"


def test_dispatch_retries_activation_then_durable_runtime_ack():
    binding = imported()
    runtime(binding)
    ingest([event()])
    claim = claim_inbound()
    delivered = []
    def handler(request):
        if request.url.path.endswith("admit"):
            return httpx.Response(200,json={"allowed":True})
        delivered.append(request.read())
        return httpx.Response(200,json={"accepted":True})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        dispatch(claim,client)
        dispatch(claim,client)
    with Session(engine()) as db:
        assert db.get(InboxEvent,claim.id).state == "done"
    assert len(delivered)==1


def require_postgres():
    if engine().dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL row locks")


def test_concurrent_claims_serialize_conversation_and_fence_old_worker():
    require_postgres()
    imported()
    ingest([event(1),event(2)],after="2")
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _:claim_inbound(),range(8)))
    claims = [c for c in claims if c]
    assert len(claims)==1
    stale = claims[0]
    with Session(engine()) as db:
        row = db.get(InboxEvent,stale.id)
        row.lease_until=now()-1
        binding=db.get(Binding,row.binding_id)
        binding.processing_lease_until=now()-1
        db.add(row); db.add(binding); db.commit()
    recover_leases()
    replacement=claim_inbound()
    assert replacement.id==stale.id and replacement.fence>stale.fence
    finish_inbound(stale,"done")
    with Session(engine()) as db:
        assert db.get(InboxEvent,stale.id).state=="claimed"
    finish_inbound(replacement,"done")
    assert claim_inbound().id != stale.id


def test_concurrent_global_queue_capacity_across_tenants():
    require_postgres()
    for i in range(8):
        imported(str(i),f"1555000000{i}@s.whatsapp.net")
    settings().queue_cap_total=3
    def enqueue(i):
        with Session(engine()) as db:
            lock_tenant(db,str(i))
            binding=db.exec(select(Binding).where(Binding.tenant_id==str(i))).one()
            try:
                queue_text(db,tenant_id=str(i),binding=binding,response_key="one",text="hello")
                db.commit()
                return True
            except HTTPException as error:
                assert error.status_code==429
                return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        result=list(pool.map(enqueue,range(8)))
    assert sum(result)==3


def test_concurrent_initial_capture_commits_once():
    require_postgres()
    imported()
    ev=event()
    with ThreadPoolExecutor(max_workers=6) as pool:
        results=list(pool.map(lambda _:ingest([ev]),range(6)))
    assert sum(not r.get("duplicate",False) for r in results)==1
    with Session(engine()) as db:
        assert len(db.exec(select(InboxEvent)).all())==1
        assert len(db.exec(select(Observation)).all())==1
