import httpx
import pytest
from sqlmodel import Session, select
from messaging.db import engine
from messaging.models import Binding, ControlCommand, GroupClaim, InboxEvent
from messaging.service import create_group_claim, now
from messaging.worker import claim_inbound, dispatch, finish_inbound
from test_messaging import event, ingest, imported, runtime

OWNER="15550000001@s.whatsapp.net"
GROUP="120333444@g.us"


def setup():
    imported();binding=imported(kind="group",phone=GROUP,owner=OWNER)
    with Session(engine()) as db:
        claim=create_group_claim(db,"owner","group-claim-operation");db.commit()
        floor=db.get(Binding,binding).activated_at
    ingest([event(1,"earlier request",chat=GROUP,chat_type="group",sender_jid=OWNER)])
    with Session(engine()) as db:earlier=db.exec(select(InboxEvent)).one().id
    return binding,claim,earlier,floor


def client(admin=True,present=True):
    participants=([{ "jid":OWNER,"admin":admin}] if present else [])+[{"jid":"15550000002@s.whatsapp.net","admin":True}]
    return httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,json={"jid":GROUP,"fetched_at":now(),"participants":participants})))


def send_claim(claim,sequence=2,sender=OWNER,**kwargs):
    ingest([event(sequence,"!squire claim "+claim["nonce"],chat=GROUP,chat_type="group",sender_jid=sender,**kwargs)],str(sequence-1),str(sequence))


def test_genuine_claim_bypasses_unready_work_then_ordinary_fifo_resumes():
    binding,approval,earlier,floor=setup()
    first=claim_inbound();assert first.id==earlier
    with client() as transport:dispatch(first,transport)
    with Session(engine()) as db:
        row=db.get(InboxEvent,earlier);assert row.state=="pending" and row.error_code=="runtime_unavailable"
    send_claim(approval)
    command=claim_inbound();assert command and command.id!=earlier
    with client() as transport:dispatch(command,transport)
    with Session(engine()) as db:
        assert db.get(GroupClaim,approval["claim_id"]).state=="activated"
        assert db.get(Binding,binding).activated_at==floor
        assert db.exec(select(ControlCommand)).one().kind=="activate_group"
        assert db.get(InboxEvent,earlier).state=="pending"
        row=db.get(InboxEvent,earlier);row.next_attempt_at=0;db.add(row);db.commit()
    runtime(binding,kind="group",token="g"*40)
    ingest([event(3,"!squire later request",chat=GROUP,chat_type="group",sender_jid=OWNER)],"2","3")
    first=claim_inbound();assert first.id==earlier;finish_inbound(first,"done")
    second=claim_inbound();assert second and second.id not in {earlier,command.id}


@pytest.mark.parametrize("invalid",["nonce","sender","edited"])
def test_invalid_or_edited_claim_cannot_bypass_ordinary_fifo(invalid):
    _,approval,earlier,_=setup()
    if invalid=="nonce":approval={**approval,"nonce":"not-a-valid-nonce"}
    send_claim(approval,sender="15550000003@s.whatsapp.net" if invalid=="sender" else OWNER)
    if invalid=="edited":
        ingest([event(3,"!squire ordinary edited request",chat=GROUP,chat_type="group",sender_jid=OWNER,provider_message_id="provider-2")],"2","3")
    with Session(engine()) as db:
        row=db.exec(select(InboxEvent).where(InboxEvent.provider_message_id=="provider-2")).one()
        assert row.kind=="message" and db.exec(select(ControlCommand)).all()==[]
    assert claim_inbound().id==earlier


def test_expired_claim_never_activates_or_consumes_pending_work(monkeypatch):
    _,approval,earlier,_=setup();deadline=int(now())+5
    send_claim(approval,expires_at=deadline)
    from messaging import worker
    monkeypatch.setattr(worker,"now",lambda:deadline+1)
    command=claim_inbound();assert command.id!=earlier
    with client() as transport:dispatch(command,transport)
    with Session(engine()) as db:
        assert db.get(InboxEvent,command.id).state=="expired"
        assert db.get(InboxEvent,earlier).state=="pending"
        assert db.get(GroupClaim,approval["claim_id"]).state=="pending"


def test_owner_removal_revokes_even_before_runtime_exists():
    binding,_,earlier,_=setup()
    with client(present=False) as transport:dispatch(claim_inbound(),transport)
    with Session(engine()) as db:
        assert not db.get(Binding,binding).active
        assert db.get(InboxEvent,earlier).state=="cancelled"


def test_non_admin_approval_has_no_activation_or_message_side_effects():
    binding,approval,earlier,floor=setup();send_claim(approval)
    with client(admin=False) as transport:dispatch(claim_inbound(),transport)
    with Session(engine()) as db:assert db.get(GroupClaim,approval["claim_id"]).state=="awaiting_admin"
    ingest([event(3,"!squire approve "+approval["nonce"],chat=GROUP,chat_type="group",sender_jid="15550000003@s.whatsapp.net")],"2","3")
    with client(admin=False) as transport:dispatch(claim_inbound(),transport)
    with Session(engine()) as db:
        assert db.get(GroupClaim,approval["claim_id"]).state=="awaiting_admin"
        assert db.exec(select(ControlCommand)).all()==[]
        assert db.get(InboxEvent,earlier).state=="pending"


    ingest([event(4,"!squire approve "+approval["nonce"],chat=GROUP,chat_type="group",sender_jid="15550000002@s.whatsapp.net")],"3","4")
    with client(admin=False) as transport:dispatch(claim_inbound(),transport)
    with Session(engine()) as db:
        assert db.get(GroupClaim,approval["claim_id"]).state=="activated"
        assert db.get(Binding,binding).activated_at==floor
        assert db.get(InboxEvent,earlier).state=="pending"


def test_valid_claim_cannot_bypass_existing_binding_processing_lease():
    _,approval,earlier,_=setup();active=claim_inbound();assert active.id==earlier
    send_claim(approval)
    assert claim_inbound() is None
    finish_inbound(active,"pending","runtime_unavailable")
    command=claim_inbound();assert command and command.id!=earlier
