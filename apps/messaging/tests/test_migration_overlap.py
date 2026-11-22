import pytest
from sqlmodel import Session, select
from messaging.db import engine
from messaging.models import InboxEvent
from test_messaging import event, ingest
from conftest import headers


def request(**changes):
    return dict(operation_id="founder-migration", tenant_id="owner", account_id="account",
        recipient="15550000001@s.whatsapp.net", aliases=["12345@lid"], activated_at=1000,
        handled_provider_message_ids=["SOURCE-HANDLED"], **changes)


def test_source_receipt_import_and_alias_replay_are_idempotent_without_content(client,monkeypatch):
    req=request()
    first=client.post("/v1/operator/bindings/import",headers=headers("operator"),json=req)
    assert first.status_code==200,first.text
    assert client.post("/v1/operator/bindings/import",headers=headers("operator"),json=req).json()==first.json()
    ingest([event(1,"PRIVATE BODY",chat="12345@lid",timestamp=1000,provider_message_id="SOURCE-HANDLED"),
            event(2,"NEW REQUEST",timestamp=1000,provider_message_id="SOURCE-UNHANDLED")])
    with Session(engine()) as db:
        rows={row.provider_message_id:row for row in db.exec(select(InboxEvent))}
        assert len(rows)==2
        assert rows["SOURCE-HANDLED"].state=="done" and rows["SOURCE-HANDLED"].payload_enc is None
        assert rows["SOURCE-UNHANDLED"].state=="pending"
    assert client.post("/v1/operator/bindings/import",headers=headers("operator"),json=req).status_code==200
    from messaging import worker
    original_now=worker.now()
    monkeypatch.setattr(worker,"now",lambda:original_now+90*86400)
    worker.retain()
    ingest([event(3,"LATE REPLAY",timestamp=1000,provider_message_id="SOURCE-HANDLED")],before="1",after="2")
    with Session(engine()) as db:
        row=db.exec(select(InboxEvent).where(InboxEvent.provider_message_id=="SOURCE-HANDLED")).one()
        assert row.state=="done" and row.payload_enc is None and row.error_code.startswith("migration_handled:")


@pytest.mark.parametrize("change",[{"account_id":"other"},{"tenant_id":"other"},{"activated_at":999},{"operation_id":"different-import"}])
def test_import_cannot_reassign_identity_boundary_or_receipt(client,change):
    req=request();assert client.post("/v1/operator/bindings/import",headers=headers("operator"),json=req).status_code==200
    assert client.post("/v1/operator/bindings/import",headers=headers("operator"),json={**req,**change}).status_code==409


@pytest.mark.parametrize("state",["pending","claimed","done","ignored","expired","cancelled"])
def test_import_never_overwrites_existing_event_disposition(client,state):
    req=request();req["handled_provider_message_ids"]=[]
    assert client.post("/v1/operator/bindings/import",headers=headers("operator"),json=req).status_code==200
    ingest([event(1,"ORIGINAL",timestamp=1000,provider_message_id="SOURCE-HANDLED")])
    with Session(engine()) as db:
        row=db.exec(select(InboxEvent)).one();row.state=state;db.add(row);db.commit();payload=row.payload_enc
    req["handled_provider_message_ids"]=["SOURCE-HANDLED"]
    assert client.post("/v1/operator/bindings/import",headers=headers("operator"),json=req).status_code==409
    with Session(engine()) as db:
        row=db.exec(select(InboxEvent)).one();assert row.state==state and row.payload_enc==payload


@pytest.mark.parametrize("ids",[[""],["x"*201],["bad\nidentity"],["x"]*1001])
def test_overlap_import_is_bounded_and_operator_only(client,ids):
    req=request();req["handled_provider_message_ids"]=ids
    assert client.post("/v1/operator/bindings/import",headers=headers("operator"),json=req).status_code==422
    assert client.post("/v1/operator/bindings/import",headers=headers("transport"),json=request()).status_code==401


def test_import_rejects_prebinding_alias_observation_atomically(client):
    ingest([event(1,"OLD UNBOUND",chat="12345@lid",timestamp=1000,provider_message_id="SOURCE-HANDLED")])
    response=client.post("/v1/operator/bindings/import",headers=headers("operator"),json=request())
    assert response.status_code==409
    from messaging.models import Binding,TenantState
    with Session(engine()) as db:
        assert db.exec(select(Binding)).all()==[] and db.exec(select(TenantState)).all()==[]
        assert db.exec(select(InboxEvent)).one().state=="ignored"


def test_operator_group_mode_preserves_reviewed_policy_through_genuine_claim(client):
    from messaging.models import Binding
    from messaging.service import create_group_claim,now
    from messaging.groups import handle_command
    from messaging.schemas import GroupSnapshot
    owner=request();assert client.post("/v1/operator/bindings/import",headers=headers("operator"),json=owner).status_code==200
    with Session(engine()) as db:
        approval=create_group_claim(db,"owner","normal-group-claim");db.commit()
    group={**owner,"operation_id":"group-migration","recipient":"120333444@g.us","aliases":[],"kind":"group","owner_recipient":owner["recipient"],"group_mode":"all","handled_provider_message_ids":[]}
    result=client.post("/v1/operator/bindings/import",headers=headers("operator"),json=group)
    assert result.status_code==200;binding=result.json()["conversation_id"]
    assert client.post("/v1/operator/bindings/import",headers=headers("operator"),json=group).status_code==200
    assert client.post("/v1/operator/bindings/import",headers=headers("operator"),json={**group,"group_mode":"addressed"}).status_code==409
    assert client.post("/v1/operator/bindings/import",headers=headers("operator"),json={**owner,"group_mode":"all"}).status_code==422
    assert client.patch("/v1/control/groups/"+binding,headers=headers("control"),json={"tenant_id":"owner","mode":"all"}).status_code==403
    with Session(engine()) as db:
        cmd=event(text="!squire claim "+approval["nonce"],chat=group["recipient"],chat_type="group",sender_jid=owner["recipient"])
        snapshot=GroupSnapshot(jid=group["recipient"],fetched_at=now(),participants=[{"jid":owner["recipient"],"admin":True}])
        assert handle_command(db,cmd,snapshot)
        row=db.get(Binding,binding);assert row.mode=="all" and row.activated_at==1000
        db.commit()
