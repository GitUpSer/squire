"""Real Postgres races; opt in with PILOT_TEST_POSTGRES_URL to a disposable DB."""
import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta, datetime
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlmodel import Session, select
from control_api.config import get_settings
from control_api import db
from control_api.models import Tenant, utcnow
from control_api.pilot_models import PilotAccount, PilotInvitation, PilotRuntime, PilotEntitlement, PilotAdmission, PilotTurn, PilotOperation
from control_api.pilot_security import digest, issue_session

@pytest.fixture
def postgres(monkeypatch):
    url=os.environ.get("PILOT_TEST_POSTGRES_URL")
    if not url: pytest.skip("set PILOT_TEST_POSTGRES_URL for real database concurrency tests")
    schema="pilot_test_"+secrets.token_hex(6)
    engine=create_engine(url)
    with engine.begin() as conn: conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    monkeypatch.setenv("DATABASE_URL",url+"?options=-csearch_path%3D"+schema)
    for key,value in {"PILOT_ENABLED":"true","PILOT_ADMISSION_OPEN":"true","PILOT_SECRET":"a-dedicated-postgres-test-secret-32-characters","PILOT_COOKIE_SECURE":"false","PILOT_WEB_ORIGIN":"http://testserver","CONTROL_MESSAGING_SERVICE_TOKEN":"messaging-test"}.items(): monkeypatch.setenv(key,value)
    get_settings.cache_clear();db.reset_engine();db.init_db()
    from control_api.main import create_app
    yield create_app()
    db.reset_engine()
    with engine.begin() as conn: conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    engine.dispose()


def test_concurrent_signups_never_oversubscribe_twenty(postgres):
    users=[]
    with Session(db.get_engine()) as session:
        for i in range(30):
            aid=f"person-{i}";email=aid+"@example.com";invitation="invite-"+aid
            session.add(PilotAccount(id=aid,email=email))
            session.add(PilotInvitation(id=aid,email=email,digest=digest("invitation",invitation),expires_at=utcnow()+timedelta(hours=1)))
            session.commit();token,csrf=issue_session(session,aid);users.append((token,csrf,invitation))
    def signup(user):
        token,csrf,invitation=user
        with TestClient(postgres) as client:
            client.cookies.set("squire_session",token)
            response=client.post("/v1/trial",headers={"origin":"http://testserver","x-csrf-token":csrf},json={"invitation":invitation,"display_name":"Customer","timezone":"UTC","consent_version":"pilot-2026-11-15"})
            assert response.status_code in {200,409},response.text
            return response.status_code
    with ThreadPoolExecutor(max_workers=15) as pool: statuses=list(pool.map(signup,users))
    assert statuses.count(200)==20
    assert statuses.count(409)==10
    with Session(db.get_engine()) as session: assert len(session.exec(select(PilotAdmission)).all())==20


def seed_runtime(active):
    with Session(db.get_engine()) as session:
        session.add(Tenant(id="owner",email="owner@example.com"));session.commit()
        session.add(PilotRuntime(id="owner",owner_tenant_id="owner",scope_id="dm",ready=True,registered=True,model_ready=active))
        session.add(PilotEntitlement(tenant_id="owner",state="active" if active else "ready_unstarted",activated_at=utcnow() if active else None,expires_at=utcnow()+timedelta(hours=72) if active else None));session.commit()


def invoke(app,i):
    with TestClient(app) as client:
        result=client.post("/v1/service/tenants/owner/admit",headers={"authorization":"Bearer messaging-test"},json={"runtime_id":"owner","operation_id":str(i)})
        assert result.status_code==200,result.text
        return result.json()


def test_concurrent_first_turn_activates_once(postgres):
    seed_runtime(False)
    with ThreadPoolExecutor(max_workers=12) as pool: decisions=list(pool.map(lambda i:invoke(postgres,i),range(30)))
    assert {d["reason"] for d in decisions}=={"activating"}
    assert len({datetime.fromisoformat(d["activated_at"]) for d in decisions})==1
    with Session(db.get_engine()) as session:
        assert len(session.exec(select(PilotOperation)).all())==1
        assert len(session.exec(select(PilotTurn)).all())==0


def test_concurrent_quota_is_exact(postgres):
    seed_runtime(True)
    assert get_settings().pilot_daily_turn_limit==75
    with ThreadPoolExecutor(max_workers=15) as pool: decisions=list(pool.map(lambda i:invoke(postgres,i),range(100)))
    with Session(db.get_engine()) as diagnostic:
        row=diagnostic.get(PilotEntitlement,"owner")
        details={"state":row.state,"daily_turns":row.daily_turns,"daily_index":row.daily_index,"lifetime_turns":row.lifetime_turns,"activation":str(row.activated_at),"revisions":row.revision,"reasons":{reason:sum(d["reason"]==reason for d in decisions) for reason in {d["reason"] for d in decisions}},"remaining":sorted({d["remaining_turns"] for d in decisions})}
    assert sum(d["allowed"] for d in decisions)==75,details
    with Session(db.get_engine()) as session:
        assert session.get(PilotEntitlement,"owner").daily_turns==75
        assert len(session.exec(select(PilotTurn)).all())==75


def test_first_turn_and_idle_pause_race_has_one_valid_winner(postgres):
    from control_api.pilot_lifecycle import admit_turn,reconcile_deadlines
    from threading import Barrier
    with Session(db.get_engine()) as session:
        session.add(Tenant(id="idle-race",email="idle-race@example.com"));session.flush()
        session.add(PilotRuntime(id="idle-race",owner_tenant_id="idle-race",scope_id="idle-race-scope",ready=True,registered=True,state="ready"))
        session.add(PilotEntitlement(tenant_id="idle-race",created_at=utcnow()-timedelta(days=2)));session.commit()
    barrier=Barrier(2)
    def first_turn():
        with Session(db.get_engine()) as session:
            barrier.wait();return admit_turn(session,"idle-race","idle-race","first-event")
    def idle_pause():
        with Session(db.get_engine()) as session:
            barrier.wait();reconcile_deadlines(session,utcnow())
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(first_turn);idle=pool.submit(idle_pause);decision=first.result();idle.result()
    with Session(db.get_engine()) as session:
        entitlement=session.get(PilotEntitlement,"idle-race")
        assert (entitlement.state,decision["reason"]) in {("activating","activating"),("setup_paused","setup_paused")}
        assert (entitlement.activated_at is not None)==(entitlement.state=="activating")
