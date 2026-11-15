import httpx
from sqlmodel import Session, select
from messaging.crypto import open_sealed
from messaging.db import engine
from messaging.models import Binding, Outbox, PhoneChallenge, Runtime, TenantState
from messaging.service import now, queue_text
from messaging.worker import claim_outbound, send
from conftest import headers
from test_messaging import imported, runtime, enable, event, ingest


def test_no_introduction_before_runtime_heartbeat(client):
    binding = imported()
    runtime(binding)
    with Session(engine()) as db:
        tenant = db.get(TenantState, "owner")
        tenant.state = "ready_unstarted"
        db.add(tenant)
        row=queue_text(db,tenant_id="owner",runtime_id="ownerdm",binding=db.get(Binding,binding),
                       response_key="introduction",text="ready")
        db.commit()
    enable()
    claim=claim_outbound()
    calls=[]
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200,json={"allowed":False,"state":"ready_unstarted","runtime_ready":False})
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        send(claim,transport)
    assert calls==["/v1/service/tenants/owner/entitlement"]
    with Session(engine()) as db:
        assert db.get(Outbox,claim.id).state=="queued"
    assert client.get("/readyz").status_code==200


def test_control_outage_never_submits_to_provider():
    binding=imported()
    with Session(engine()) as db:
        queue_text(db,tenant_id="owner",binding=db.get(Binding,binding),response_key="one",text="hello")
        db.commit()
    enable()
    claim=claim_outbound()
    calls=[]
    def handler(request):
        calls.append(request.url.path)
        raise httpx.ConnectError("unavailable")
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        send(claim,transport)
    assert calls==["/v1/service/tenants/owner/entitlement"]
    with Session(engine()) as db:
        assert db.get(Outbox,claim.id).state=="queued"


def test_receipt_before_phone_binding_updates_setup_delivery(client):
    request={"operation_id":"challenge-op","tenant_id":"new","account_id":"new-account",
             "phone":"+15550000006","consent_version":"v1"}
    result=client.post("/v1/control/phone/challenges",headers=headers("control"),json=request).json()
    with Session(engine()) as db:
        challenge=db.get(PhoneChallenge,result["challenge_id"])
        row=db.get(Outbox,challenge.outbox_id)
        row.state,row.provider_message_id="accepted","outgoing-provider-id"
        db.add(row);db.commit()
    ingest([event(kind="receipt",chat="15550000006@s.whatsapp.net",recipient_jid="15550000006@s.whatsapp.net",
                  receipt_status="read",provider_message_id="outgoing-provider-id")])
    status=client.get("/v1/control/tenants/new",headers=headers("control")).json()
    assert status["phone_challenge"]["delivery_status"]=="read"
    ingest([event(2,kind="receipt",chat="15550000006@s.whatsapp.net",recipient_jid="15550000006@s.whatsapp.net",
                  receipt_status="delivered",provider_message_id="outgoing-provider-id")],"1","2")
    assert client.get("/v1/control/tenants/new",headers=headers("control")).json()["phone_challenge"]["delivery_status"]=="read"


def test_group_approval_requires_owner_then_separate_release(client):
    private=imported()
    runtime(private)
    group=imported(phone="120123456@g.us",kind="group",owner="15550000001@s.whatsapp.net")
    runtime(group,kind="group",token="g"*40)
    result=client.post("/v1/approvals",headers=headers("group"),json={"request_key":"calendar-action","summary":"Find available meeting time"})
    assert result.status_code==200,result.text
    approval=result.json()["approval_id"]
    decision={"tenant_id":"owner","decision":"approve"}
    assert client.post(f"/v1/control/approvals/{approval}",headers=headers("group"),json=decision).status_code==401
    assert client.get("/v1/approvals",headers=headers("group")).status_code==403
    assert client.get("/v1/approvals",headers=headers("dm")).json()=={"approvals":[]}
    assert client.post(f"/v1/control/approvals/{approval}",headers=headers("control"),json=decision).status_code==200
    private_result={"tenant_id":"owner","decision":"approve","result":"Private calendar says Tuesday at noon."}
    assert client.post(f"/v1/approvals/{approval}/result",headers=headers("group"),json=private_result).status_code==403
    assert client.post(f"/v1/approvals/{approval}/result",headers=headers("dm"),json=private_result).status_code==200
    with Session(engine()) as db:
        group_rows=db.exec(select(Outbox).where(Outbox.binding_id==group)).all()
        assert len(group_rows)==1 # consent intro only, private result not released
    release={"tenant_id":"owner","decision":"release","result":"Tuesday at noon works."}
    assert client.post(f"/v1/control/approvals/{approval}",headers=headers("control"),json=release).status_code==200
    assert client.post(f"/v1/control/approvals/{approval}",headers=headers("control"),json=release).status_code==200
    with Session(engine()) as db:
        rows=db.exec(select(Outbox).where(Outbox.binding_id==group)).all()
        assert len(rows)==2
        assert any(open_sealed(r.payload_enc,"outbox:"+r.id)["text"]=="Tuesday at noon works." for r in rows)


def test_deleted_tenant_cannot_use_old_runtime_and_payloads_are_erased(client):
    binding=imported()
    runtime(binding)
    req={"conversation_id":binding,"response_key":"private-response","text":"content to erase"}
    assert client.post("/v1/outbound",headers=headers("dm"),json=req).status_code==200
    response=client.post("/v1/control/tenants/owner/state",headers=headers("control"),json={"state":"deleted"})
    assert response.status_code==200
    assert client.post("/v1/outbound",headers=headers("dm"),json=req).status_code==401
    with Session(engine()) as db:
        assert all(row.payload_enc is None for row in db.exec(select(Outbox)))
        assert not db.get(Binding,binding).active


def test_unknown_provenance_is_not_admitted():
    imported()
    ingest([event(origin="unknown")])
    from messaging.worker import claim_inbound
    assert claim_inbound() is None


def test_only_explicit_retryable_definitely_unsent_can_retry():
    binding=imported()
    with Session(engine()) as db:
        queue_text(db,tenant_id="owner",binding=db.get(Binding,binding),response_key="one",text="hello")
        db.commit()
    enable()
    claim=claim_outbound()
    def handler(request):
        if request.url.path.endswith("entitlement"):
            return httpx.Response(200,json={"allowed":True,"state":"imported_active","runtime_ready":True})
        return httpx.Response(200,json={"status":"definitely_unsent","retryable":True,"error_code":"capture_not_ready"})
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        send(claim,transport)
    with Session(engine()) as db:
        row=db.get(Outbox,claim.id)
        assert row.state=="queued" and row.next_attempt_at>now()
        row.next_attempt_at=0;db.add(row);db.commit()
    retry=claim_outbound()
    assert retry.id==claim.id and retry.fence>claim.fence


def test_unapproved_group_random_claim_does_not_retain_content_or_queue_work():
    from messaging.models import InboxEvent
    from messaging.worker import claim_inbound
    ingest([event(chat="120444555@g.us",chat_type="group",text="!squire claim random-nonce")])
    assert claim_inbound() is None
    with Session(engine()) as db:
        assert db.exec(select(InboxEvent)).one().payload_enc is None


def test_capture_ack_is_not_sent_before_commit(client, monkeypatch):
    from sqlalchemy.exc import IntegrityError
    from messaging.models import Checkpoint
    original=Session.commit
    def rejected_commit(db):
        if any(isinstance(row,Checkpoint) for row in db.dirty):
            raise IntegrityError("fixture commit failure", {}, RuntimeError("fixture"))
        return original(db)
    monkeypatch.setattr(Session,"commit",rejected_commit)
    result=client.post("/v1/ingest/batches",headers=headers("transport"),json={
        "provider_account_id":"squire","store_id":"commit-test","checkpoint_after":"1",
        "events":[event().model_dump()]})
    assert result.status_code==409
    with Session(engine()) as db:
        assert db.get(Checkpoint,"commit-test") is None


def test_group_revoke_cancels_egress_and_durably_revokes_runtime(client):
    from messaging.models import ControlCommand
    from messaging.worker import deliver_control_commands
    private=imported()
    group=imported(phone="120123456@g.us",kind="group",owner="15550000001@s.whatsapp.net")
    runtime(group,kind="group",token="g"*40)
    revoked=client.patch(f"/v1/control/groups/{group}",headers=headers("control"),json={"tenant_id":"owner","active":False})
    assert revoked.status_code==200
    client.patch(f"/v1/control/groups/{group}",headers=headers("control"),json={"tenant_id":"owner","active":False})
    with Session(engine()) as db:
        assert all(row.state=="cancelled" for row in db.exec(select(Outbox).where(Outbox.binding_id==group)))
        commands=db.exec(select(ControlCommand)).all()
        assert len(commands)==1 and commands[0].binding_generation==2
    requests=[]
    def callback(request):
        import json
        requests.append(json.loads(request.read()))
        assert request.url.path=="/v1/service/groups/state"
        return httpx.Response(503 if len(requests)==1 else 200,json={"accepted":True})
    with httpx.Client(transport=httpx.MockTransport(callback)) as transport:
        deliver_control_commands(transport)
        with Session(engine()) as db:
            row=db.exec(select(ControlCommand)).one()
            assert row.state=="pending"
            row.next_attempt_at=0;db.add(row);db.commit()
        deliver_control_commands(transport)
    assert requests[0]==requests[1]=={"tenant_id":"owner","conversation_id":group,"state":"paused","binding_generation":2}


def test_group_revoke_cancels_private_approvals_and_reapproval_cannot_resurrect(client):
    from messaging.models import ShareApproval
    from messaging.groups import revoke_group
    private=imported()
    runtime(private)
    group=imported(phone="120123456@g.us",kind="group",owner="15550000001@s.whatsapp.net")
    runtime(group,kind="group",token="g"*40)
    response=client.post("/v1/approvals",headers=headers("group"),json={"request_key":"cancel-on-revoke","summary":"Read my private calendar"})
    approval=response.json()["approval_id"]
    assert client.post(f"/v1/control/approvals/{approval}",headers=headers("control"),json={"tenant_id":"owner","decision":"approve"}).status_code==200
    assert len(client.get("/v1/approvals",headers=headers("dm")).json()["approvals"])==1
    with Session(engine()) as db:
        revoke_group(db,db.get(Binding,group));db.commit()
        assert db.get(ShareApproval,approval).state=="denied"
    assert client.get("/v1/approvals",headers=headers("dm")).json()=={"approvals":[]}
    with Session(engine()) as db:
        binding=db.get(Binding,group);binding.active=True;db.add(binding);db.commit()
    assert client.get("/v1/approvals",headers=headers("dm")).json()=={"approvals":[]}
