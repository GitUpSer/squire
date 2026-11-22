import json
import time
import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlmodel import Session, select
from messaging import ingest as ingest_module, media, service, worker
from messaging.crypto import open_sealed
from messaging.db import engine, migrate, verify_schema
from messaging.models import InboxEvent, Outbox, ProviderAccount, SchemaRevision
from messaging.schemas import Event
from conftest import headers
from test_messaging import event, imported, ingest, runtime, enable


@pytest.fixture
def clock(monkeypatch):
    current = [int(time.time()) + 2]
    for module in (ingest_module, media, service, worker):
        monkeypatch.setattr(module, 'now', lambda: current[0])
    return current


def stored_event():
    with Session(engine()) as db:
        return db.exec(select(InboxEvent).where(InboxEvent.kind == 'message')).one()


def setup_event(clock, deadline=None, **kwargs):
    binding = imported(); runtime(binding)
    ingest([event(timestamp=clock[0], expires_at=deadline, **kwargs)])
    return binding, stored_event().id


def test_message_expiry_requires_explicit_null_or_positive_integer():
    payload = event().model_dump(); payload.pop('expires_at')
    with pytest.raises(ValidationError): Event.model_validate(payload)
    for value in (0, -1, True, 123.0, '123', float('inf'), 2**63):
        with pytest.raises(ValidationError): Event.model_validate(dict(payload, expires_at=value))
    assert Event.model_validate(dict(payload, expires_at=None)).expires_at is None
    assert Event.model_validate(dict(payload, expires_at=123)).expires_at == 123
    assert Event.model_validate(dict(payload, kind='delete')).kind == 'delete'


def test_revision_one_upgrade_preserves_rows_but_does_not_infer_ordinary_expiry(clock):
    setup_event(clock)
    before = stored_event()
    with Session(engine()) as db:
        for state in ('queued','claimed','dispatching','uncertain'):
            db.add(Outbox(id='legacy-'+state,response_key='legacy-'+state,tenant_id='owner',
                runtime_id='ownerdm',state=state,payload_enc='encrypted-legacy',created_at=clock[0],expires_at=clock[0]+100))
        db.add(Outbox(id='challenge',response_key='challenge',tenant_id='owner',
            challenge_id='explicit-system-challenge',payload_enc='encrypted-challenge',created_at=clock[0],expires_at=clock[0]+100))
        db.commit()
    with engine().begin() as conn:
        conn.execute(text('DROP INDEX ix_outbox_message_cause_event_id'))
        conn.execute(text('ALTER TABLE outbox_message DROP COLUMN cause_event_id'))
        conn.execute(text('ALTER TABLE inbox_event DROP COLUMN expires_at'))
        conn.execute(text('UPDATE schema_revision SET version=1 WHERE id=1'))
    migrate(); migrate(); verify_schema()
    after = stored_event()
    assert after.id == before.id and after.payload_enc == before.payload_enc
    assert after.expires_at == 0
    with Session(engine()) as db:
        assert db.get(SchemaRevision, 1).version == 2
        for state in ('queued','claimed','dispatching','uncertain'):
            row=db.get(Outbox,'legacy-'+state)
            assert row.state == ('uncertain' if state in {'dispatching','uncertain'} else 'cancelled')
            assert row.payload_enc is None and row.expires_at == 0
        assert db.get(Outbox,'challenge').state == 'queued'
        assert db.get(Outbox,'challenge').payload_enc == 'encrypted-challenge'
    with httpx.Client(transport=httpx.MockTransport(lambda req: pytest.fail('legacy event dispatched'))) as client:
        worker.dispatch(worker.claim_inbound(), client)
    assert stored_event().state == 'expired' and stored_event().payload_enc is None


def test_expired_ingress_and_queued_expiry_never_dispatch(clock):
    setup_event(clock, clock[0])
    assert stored_event().state == 'expired' and stored_event().payload_enc is None
    assert worker.claim_inbound() is None
    ingest([event(2, timestamp=clock[0], expires_at=clock[0]+1)], '1', '2')
    claim = worker.claim_inbound(); clock[0] += 1
    with httpx.Client(transport=httpx.MockTransport(lambda req: pytest.fail('expired event dispatched'))) as client:
        worker.dispatch(claim, client)
    with Session(engine()) as db:
        row = db.get(InboxEvent, claim.id)
        assert row.state == 'expired' and row.payload_enc is None


def test_deadline_forwarded_and_rechecked_after_admission(clock):
    _, eid = setup_event(clock, clock[0]+5)
    sent = []
    def handler(req):
        sent.append(req.url.path)
        clock[0] += 5
        return httpx.Response(200, json={'allowed': True})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        worker.dispatch(worker.claim_inbound(), client)
    assert len(sent) == 1 and sent[0].endswith('/admit')
    assert stored_event().state == 'expired'


def test_replay_cannot_extend_deadline_and_dispatch_carries_minimum(clock):
    deadline = clock[0]+20
    setup_event(clock, deadline)
    ingest([event(2, expires_at=None, provider_message_id='provider-1')], '1', '2')
    ingest([event(3, expires_at=deadline-5, provider_message_id='provider-1')], '2', '3')
    assert stored_event().expires_at == deadline-5
    assert open_sealed(stored_event().payload_enc, 'inbox:'+stored_event().id)['expires_at'] == deadline-5
    delivered = []
    def handler(req):
        if req.url.path.endswith('/admit'): return httpx.Response(200, json={'allowed': True})
        delivered.append(json.loads(req.content)['events'][0])
        return httpx.Response(200, json={'accepted': True})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        worker.dispatch(worker.claim_inbound(), client)
    assert delivered[0]['payload']['expires_at'] == deadline-5


def test_media_expiry_before_and_during_fetch(client, monkeypatch, clock):
    _, eid = setup_event(clock, clock[0]+2, media={'type':'image'})
    fetched = []
    def handler(req):
        fetched.append(req); clock[0] += 2
        return httpx.Response(200, content=b'expired private bytes')
    monkeypatch.setattr(media, 'broker_client', lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.get('/v1/media/'+eid, headers=headers('dm')).status_code == 404
    assert len(fetched) == 1
    assert client.get('/v1/media/'+eid, headers=headers('dm')).status_code == 404
    assert len(fetched) == 1


def test_reply_deadline_is_authoritative_and_rechecked_after_edit(client, clock):
    deadline = clock[0]+20
    binding, eid = setup_event(clock, deadline)
    body = {'conversation_id':binding, 'response_key':'reply', 'text':'answer', 'cause_event_id':eid}
    response = client.post('/v1/outbound', headers=headers('dm'), json=body)
    assert response.status_code == 200
    with Session(engine()) as db:
        reply = db.get(Outbox, response.json()['operation_id'])
        assert reply.expires_at == deadline and reply.cause_event_id == eid
    ingest([event(2, provider_message_id='provider-1', expires_at=clock[0]+1)], '1', '2')
    clock[0] += 1
    assert client.post('/v1/outbound', headers=headers('dm'), json=dict(body,response_key='late')).status_code == 410
    enable(); claim = worker.claim_outbound(); calls = []
    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(200, json={'allowed':True,'runtime_ready':True,'state':'imported_active'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as broker: worker.send(claim, broker)
    assert all(path.endswith('/entitlement') for path in calls)
    with Session(engine()) as db: assert db.get(Outbox, claim.id).state == 'cancelled'


def test_outbound_forwards_deadline_and_rejects_changed_parent(client, clock):
    binding, eid = setup_event(clock, clock[0]+20)
    body = {'conversation_id':binding,'response_key':'reply','text':'answer','cause_event_id':eid}
    assert client.post('/v1/outbound', headers=headers('dm'), json=body).status_code == 200
    assert client.post('/v1/outbound', headers=headers('dm'), json=dict(body,cause_event_id=None)).status_code == 409
    enable(); sent = []
    def handler(req):
        if req.url.path.endswith('/entitlement'): return httpx.Response(200,json={'allowed':True,'runtime_ready':True,'state':'imported_active'})
        sent.append(json.loads(req.content)); return httpx.Response(200,json={'status':'accepted','provider_message_id':'sent'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as broker: worker.send(worker.claim_outbound(), broker)
    assert sent[0]['expires_at'] == clock[0]+20


def test_delete_tombstone_blocks_late_reply_and_later_message(client, clock):
    binding, eid = setup_event(clock)
    with Session(engine()) as db:
        row=db.get(InboxEvent,eid); row.state='done'; db.add(row); db.commit()
    ingest([event(2,kind='delete',provider_message_id='provider-1')], '1', '2')
    body={'conversation_id':binding,'response_key':'reply','text':'answer','cause_event_id':eid}
    assert client.post('/v1/outbound',headers=headers('dm'),json=body).status_code == 410
    ingest([event(3,kind='delete',provider_message_id='provider-future')], '2', '3')
    ingest([event(4,provider_message_id='provider-future')], '3', '4')
    with Session(engine()) as db:
        row=db.exec(select(InboxEvent).where(InboxEvent.provider_message_id=='provider-future',InboxEvent.kind=='message')).one()
        assert row.state == 'cancelled' and row.payload_enc is None


def test_retention_clears_expired_payload_without_clearing_ordinary(clock):
    setup_event(clock, clock[0]+1)
    ingest([event(2)], '1', '2'); clock[0] += 1
    worker.retain()
    with Session(engine()) as db:
        rows=db.exec(select(InboxEvent).order_by(InboxEvent.provider_message_id)).all()
        assert rows[0].payload_enc is None and rows[0].state == 'expired'
        assert rows[1].payload_enc is not None and rows[1].expires_at is None


@pytest.mark.parametrize('outcome', ['accepted', 'timeout', 'definitely_unsent'])
def test_expiry_during_provider_request_preserves_ambiguous_outcome(client, clock, outcome):
    binding, eid = setup_event(clock, clock[0]+2)
    body={'conversation_id':binding,'response_key':'reply','text':'answer','cause_event_id':eid}
    assert client.post('/v1/outbound',headers=headers('dm'),json=body).status_code == 200
    enable(); claim=worker.claim_outbound()
    def handler(req):
        if req.url.path.endswith('/entitlement'):
            return httpx.Response(200,json={'allowed':True,'runtime_ready':True,'state':'imported_active'})
        clock[0] += 2; worker.retain()
        with Session(engine()) as db:
            row=db.get(Outbox,claim.id)
            assert row.state == 'uncertain' and row.payload_enc is None
            assert db.get(ProviderAccount,'squire').send_owner == claim.owner
        if outcome == 'timeout': raise httpx.ReadTimeout('response lost')
        if outcome == 'definitely_unsent':
            return httpx.Response(200,json={'status':'definitely_unsent','retryable':True})
        return httpx.Response(200,json={'status':'accepted','provider_message_id':'accepted-before-expiry'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as broker: worker.send(claim,broker)
    with Session(engine()) as db:
        row=db.get(Outbox,claim.id)
        assert row.state == {'accepted':'accepted','timeout':'uncertain','definitely_unsent':'cancelled'}[outcome]
        assert row.payload_enc is None and db.get(ProviderAccount,'squire').send_owner is None
