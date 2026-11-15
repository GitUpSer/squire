"""Idle setup and explicit, audited extensions preserve lifecycle/budget boundaries."""
from datetime import timedelta
from unittest.mock import Mock
import httpx
import pytest
from sqlmodel import Session,select
from control_api.db import get_engine
from control_api.models import Tenant,ProvisionJob,ProvisionStep,utcnow,JobStatus
from control_api.pilot_models import PilotEntitlement,PilotRuntime,PilotOperation,PilotExtension,PilotAccount,PilotAdmission
from control_api.pilot_lifecycle import reconcile_deadlines,apply_extension,resume_runtimes,run_lifecycle


def seed(session,identifier,days,state="ready_unstarted",activated=False):
    tenant=Tenant(id=identifier,email=identifier+"@example.com",railway_service_id="service-"+identifier)
    session.add(tenant);session.flush()
    ent=PilotEntitlement(tenant_id=identifier,state=state,created_at=utcnow()-timedelta(days=days),activated_at=utcnow() if activated else None)
    session.add(ent);session.add(PilotRuntime(id=identifier,owner_tenant_id=identifier,scope_id="scope-"+identifier,state="ready",ready=True,registered=True))
    session.commit();return ent


def test_unstarted_deadlines_do_not_expire_active_founder_or_recent_resume():
    with Session(get_engine()) as session:
        seed(session,"idle",2);seed(session,"abandoned",8);seed(session,"founder",30,"imported_active",True)
        seed(session,"resumed",2)
        session.add(PilotOperation(id="resume-new",account_id="resumed",kind="setup_resume",reference="resumed"));session.commit()
        reconcile_deadlines(session,utcnow())
        assert session.get(PilotEntitlement,"idle").state=="setup_paused"
        assert session.get(PilotEntitlement,"abandoned").state=="deleted"
        assert session.get(PilotEntitlement,"founder").state=="imported_active"
        assert session.get(PilotEntitlement,"resumed").state=="ready_unstarted"
        assert {op.kind for op in session.exec(select(PilotOperation)).all()}=={"setup_resume","setup_paused","abandoned"}


def test_resume_preserves_trial_clock_and_rotates_runtime(monkeypatch):
    monkeypatch.setattr("control_api.pilot_lifecycle.MessagingClient",Mock())
    with Session(get_engine()) as session:
        ent=seed(session,"resume",2,"setup_resuming")
        runtime=session.get(PilotRuntime,"resume");runtime.state="paused";session.add(runtime);session.commit()
        resume_runtimes(session,PilotOperation(id="resume",account_id="resume",kind="setup_resume",reference="resume"),Mock())
        session.refresh(ent);session.refresh(runtime)
        assert ent.state=="ready_unstarted" and ent.activated_at is None and ent.expires_at is None
        assert runtime.state=="provisioning" and not runtime.ready and runtime.generation==2
        assert session.exec(select(ProvisionJob)).one().step==ProvisionStep.SET_VARIABLES


def test_extension_preserves_spend_and_total_counters(monkeypatch,respx_mock):
    from control_api.config import get_settings
    settings=get_settings();monkeypatch.setattr(settings,"litellm_base_url","https://proxy.test");monkeypatch.setattr(settings,"litellm_master_key","master-test")
    client=Mock();client.trial.delete_trial_key.return_value=True
    monkeypatch.setattr("control_api.pilot_lifecycle.MessagingClient",Mock())
    info=respx_mock.get("https://proxy.test/team/info").mock(side_effect=[httpx.Response(200,json={"team_info":{"team_id":"squire-pilot-extended","max_budget":10,"spend":8}}),httpx.Response(200,json={"team_info":{"team_id":"squire-pilot-extended","max_budget":15,"spend":8}})])
    changed=respx_mock.post("https://proxy.test/team/update").respond(200,json={})
    with Session(get_engine()) as session:
        ent=seed(session,"extended",5,"extending",True);ent.daily_turns=40;ent.lifetime_turns=170;session.add(ent)
        audit=PilotExtension(id="extension-one",tenant_id=ent.tenant_id,operator_label="pilot-operator",reason_code="pilot_evaluation",expires_at=utcnow()+timedelta(days=2),previous_budget_cents=1000,budget_cents=1500)
        session.add(audit);session.commit()
        apply_extension(session,PilotOperation(id=audit.id,account_id=ent.tenant_id,kind="extension",reference=ent.tenant_id),client)
        session.refresh(ent);session.refresh(audit)
        assert ent.state=="active" and ent.model_budget_cents==1500 and ent.daily_turns==40 and ent.lifetime_turns==170
        assert audit.state=="applied" and audit.applied_at
        import json
        assert json.loads(changed.calls[0].request.content)=={"team_id":"squire-pilot-extended","max_budget":15}
        assert info.call_count==2


def test_abandoned_cleanup_releases_seat_only_after_teardown(monkeypatch):
    fake=Mock();fake.trial.delete_trial_key.return_value=True
    monkeypatch.setattr("control_api.provisioning.ProvisionClients.build",lambda:fake)
    monkeypatch.setattr("control_api.pilot_lifecycle.MessagingClient",Mock())
    calls=[]
    def delete(session,tenant_id,clients):calls.append(tenant_id)
    monkeypatch.setattr("control_api.provisioning.delete_tenant",delete)
    with Session(get_engine()) as session:
        seed(session,"unused",8)
        session.add(PilotAccount(id="account",email="unused@example.com",tenant_id="unused"));session.flush()
        session.add(PilotAdmission(account_id="account",state="accepted",reserved_until=utcnow()));session.commit()
    run_lifecycle()
    with Session(get_engine()) as session:
        assert calls==["unused"] and session.get(PilotAdmission,"account").state=="released"
        assert session.get(PilotRuntime,"unused").state=="deleted"


def test_provisioning_cannot_deploy_paused_pilot():
    from control_api.provisioning import advance_job
    with Session(get_engine()) as session:
        seed(session,"paused",1,"paused")
        job=ProvisionJob(id="job-paused",tenant_id="paused",step=ProvisionStep.DEPLOY);session.add(job);session.commit()
        clients=Mock();result=advance_job(session,job.id,clients)
        assert result.status==JobStatus.FAILED
        clients.railway.deploy.assert_not_called()
