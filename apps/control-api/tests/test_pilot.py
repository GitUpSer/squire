"""Pilot boundaries exercise the real HTTP routes and SQL transactions."""
import re
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select
from control_api.config import get_settings
from control_api.db import get_engine
from control_api.models import Tenant, utcnow
from control_api.pilot_models import PilotAccount, PilotAdmission, PilotEntitlement, PilotRuntime, PilotTurn, PilotInvitation, PilotAuthChallenge
from control_api.pilot_security import digest, issue_session, runtime_token

@pytest.fixture
def pilot(monkeypatch):
    for key,value in {"PILOT_ENABLED":"true","PILOT_ADMISSION_OPEN":"true","PILOT_SECRET":"pilot-test-secret-that-is-at-least-32-characters","PILOT_COOKIE_SECURE":"false","PILOT_WEB_ORIGIN":"http://testserver","CONTROL_MESSAGING_SERVICE_TOKEN":"messaging-only"}.items(): monkeypatch.setenv(key,value)
    get_settings.cache_clear()
    from control_api.main import create_app
    client=TestClient(create_app())
    client.headers["origin"]="http://testserver"
    return client


def account_client(client, email="customer@example.com"):
    with Session(get_engine()) as session:
        account=PilotAccount(id=email,email=email)
        session.add(account);session.commit()
        token,csrf=issue_session(session,account.id)
    client.cookies.set("squire_session",token)
    client.headers["x-csrf-token"]=csrf
    return email


def invitation(email):
    token="invite-"+email
    with Session(get_engine()) as session:
        session.add(PilotInvitation(id=token,email=email,digest=digest("invitation",token),expires_at=utcnow()+timedelta(days=1)));session.commit()
    return token


def profile(token):
    return {"invitation":token,"display_name":"A Customer","timezone":"America/Los_Angeles","consent_version":"pilot-2026-11-15"}


def runtime_state(session,tenant="tenant-a",runtime="tenant-a",kind="dm",active=True):
    if not session.get(Tenant,tenant): session.add(Tenant(id=tenant,email=tenant+"@example.com"));session.commit()
    if runtime!=tenant: session.add(Tenant(id=runtime,email=runtime+"@example.com"));session.commit()
    session.add(PilotRuntime(id=runtime,owner_tenant_id=tenant,scope_id="scope-"+runtime,kind=kind,ready=True,registered=True,model_ready=active))
    if not session.get(PilotEntitlement,tenant): session.add(PilotEntitlement(tenant_id=tenant,state="active" if active else "ready_unstarted",activated_at=utcnow() if active else None,expires_at=utcnow()+timedelta(hours=72) if active else None))
    session.commit()


def test_login_code_one_use_and_csrf(pilot,monkeypatch):
    sent=[]
    monkeypatch.setattr("control_api.routers.pilot.send_auth_email",lambda email,code,operation_id: sent.append(code))
    token=invitation("first@example.com")
    start=pilot.post("/v1/auth/start",json={"email":"first@example.com","invitation":token})
    assert start.status_code==200
    assert len(sent)==1
    payload={"challenge_id":start.json()["challenge_id"],"code":sent[0]}
    verified=pilot.post("/v1/auth/verify",json=payload)
    assert verified.status_code==200
    assert "HttpOnly" in verified.headers["set-cookie"]
    assert pilot.post("/v1/auth/verify",json=payload).status_code==400
    assert pilot.post("/v1/trial",json=profile(token)).status_code==403
    pilot.headers["x-csrf-token"]=verified.json()["csrf_token"]
    assert pilot.get("/v1/me").json()["csrf_token"]==verified.json()["csrf_token"]
    assert pilot.post("/v1/trial",json=profile(token)).status_code==200
    pilot.headers["origin"]="https://attacker.invalid"
    assert pilot.post("/v1/trial",json=profile(token)).status_code==403


def test_invitation_bound_to_verified_email(pilot):
    account_client(pilot)
    other=invitation("someone-else@example.com")
    assert pilot.post("/v1/trial",json=profile(other)).status_code==403
    with Session(get_engine()) as session: assert session.exec(select(Tenant)).all()==[]


def test_capacity_is_atomic_and_expired_reservations_release(pilot,monkeypatch):
    monkeypatch.setenv("PILOT_SEAT_LIMIT","1");get_settings.cache_clear()
    account_client(pilot)
    token=invitation("customer@example.com")
    assert pilot.post("/v1/trial",json=profile(token)).status_code==200
    account_client(pilot,"second@example.com")
    token2=invitation("second@example.com")
    assert pilot.post("/v1/trial",json=profile(token2)).status_code==409
    with Session(get_engine()) as session:
        admission=session.get(PilotAdmission,"customer@example.com");admission.reserved_until=utcnow()-timedelta(seconds=1);session.add(admission);session.commit()
    assert pilot.post("/v1/trial",json=profile(token2)).status_code==200


def test_email_delivery_fails_closed(pilot,monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY","");get_settings.cache_clear()
    token=invitation("first@example.com")
    response=pilot.post("/v1/auth/start",json={"email":"first@example.com","invitation":token})
    assert response.status_code==503
    assert "code" not in response.json()


def test_phone_verification_queues_botless_runtime(pilot,monkeypatch):
    aid=account_client(pilot);token=invitation("customer@example.com")
    tid=pilot.post("/v1/trial",json=profile(token)).json()["tenant_id"]
    monkeypatch.setattr("control_api.routers.pilot.MessagingClient.request",lambda *a,**kw:{"tenant_id":tid,"conversation_id":"opaque-dm","verified":True})
    result=pilot.post("/v1/phone/verify",json={"challenge_id":"challenge","code":"123456"})
    assert result.status_code==200
    with Session(get_engine()) as session:
        assert session.get(Tenant,tid).bot_id is None
        assert session.get(PilotRuntime,tid).scope_id=="opaque-dm"
        assert session.get(PilotEntitlement,tid).activated_at is None
        assert session.get(PilotAdmission,aid).state=="accepted"


def test_messaging_and_runtime_principals_are_distinct(pilot):
    with Session(get_engine()) as session: runtime_state(session)
    pilot.headers["authorization"]="Bearer "+runtime_token("tenant-a","control")
    assert pilot.get("/v1/service/tenants/tenant-a/entitlement").status_code==401
    assert pilot.post("/v1/runtime/tenant-a/heartbeat",json={"ready":True,"model_ready":True}).status_code==200
    assert pilot.post("/v1/runtime/tenant-b/heartbeat",json={"ready":True}).status_code==403
    assert pilot.get("/internal/fleet").status_code==401


def test_turns_share_quota_across_groups_and_retry_idempotently(pilot,monkeypatch):
    monkeypatch.setenv("PILOT_DAILY_TURN_LIMIT","2");get_settings.cache_clear()
    with Session(get_engine()) as session:
        runtime_state(session);runtime_state(session,runtime="group-a",kind="group")
    pilot.headers["authorization"]="Bearer messaging-only"
    url="/v1/service/tenants/tenant-a/admit"
    assert pilot.post(url,json={"runtime_id":"tenant-a","operation_id":"one"}).json()["allowed"]
    assert pilot.post(url,json={"runtime_id":"tenant-a","operation_id":"one"}).json()["allowed"]
    assert pilot.post(url,json={"runtime_id":"group-a","operation_id":"one"}).json()["reason"]=="operation_scope_mismatch"
    assert pilot.post(url,json={"runtime_id":"group-a","operation_id":"two"}).json()["allowed"]
    assert pilot.post(url,json={"runtime_id":"tenant-a","operation_id":"three"}).json()["reason"]=="daily_limit"
    with Session(get_engine()) as session: assert len(session.exec(select(PilotTurn)).all())==2


def test_first_turn_starts_trial_but_waits_for_credentials(pilot):
    with Session(get_engine()) as session: runtime_state(session,active=False)
    pilot.headers["authorization"]="Bearer messaging-only"
    result=pilot.post("/v1/service/tenants/tenant-a/admit",json={"runtime_id":"tenant-a","operation_id":"one"}).json()
    assert result["allowed"] is False and result["reason"]=="activating"
    assert result["activated_at"] and result["expires_at"]
    with Session(get_engine()) as session:
        assert session.get(PilotEntitlement,"tenant-a").daily_turns==0
        assert session.exec(select(PilotTurn)).all()==[]


def test_pause_denies_turns_before_cleanup(pilot):
    aid=account_client(pilot)
    with Session(get_engine()) as session:
        runtime_state(session);account=session.get(PilotAccount,aid);account.tenant_id="tenant-a";session.add(account);session.commit()
    assert pilot.post("/v1/account/state",json={"state":"paused"}).status_code==200
    pilot.headers["authorization"]="Bearer messaging-only"
    assert pilot.post("/v1/service/tenants/tenant-a/admit",json={"runtime_id":"tenant-a","operation_id":"one"}).json()["reason"]=="paused"


def test_group_runtime_does_not_receive_master_credentials(pilot):
    from control_api.pilot_lifecycle import set_runtime_variables
    with Session(get_engine()) as session:
        runtime_state(session);runtime_state(session,runtime="group-a",kind="group")
        tenant=session.get(Tenant,"group-a");tenant.railway_service_id="service";tenant.dek_set=True
        clients=Mock();clients.railway.get_variable_names.return_value=["SQUIRE_DEK"];clients.railway.ensure_runtime_bucket.return_value="backup-runtime"
        set_runtime_variables(session,tenant,clients)
        variables=clients.railway.set_variables.call_args.args[1]
        assert variables["SQUIRE_RUNTIME_KIND"]=="group"
        assert variables["SQUIRE_DISABLE_PRIVATE_TOOLS"]=="true"
        assert "INTERNAL_API_TOKEN" not in variables
        assert variables["ANTHROPIC_API_KEY"] == ""  # clear any revoked key on reused group runtime
        assert "SQUIRE_DEK" not in variables
        assert variables["SQUIRE_CONTROL_TOKEN"]!=runtime_token("tenant-a","control")



def test_superseded_heartbeat_cannot_restore_readiness(pilot):
    with Session(get_engine()) as session:
        runtime_state(session)
        runtime=session.get(PilotRuntime,"tenant-a");runtime.deployment_generation=2;runtime.ready=False;session.add(runtime);session.commit()
    pilot.headers["authorization"]="Bearer "+runtime_token("tenant-a","control")
    assert pilot.post("/v1/runtime/tenant-a/heartbeat",json={"ready":True,"model_ready":True,"deployment_generation":1}).status_code==409
    with Session(get_engine()) as session: assert not session.get(PilotRuntime,"tenant-a").ready


def test_expiry_on_admission_still_queues_cleanup(pilot):
    from control_api.pilot_models import PilotOperation
    with Session(get_engine()) as session:
        runtime_state(session)
        ent=session.get(PilotEntitlement,"tenant-a");ent.expires_at=utcnow()-timedelta(seconds=1);session.add(ent);session.commit()
    pilot.headers["authorization"]="Bearer messaging-only"
    result=pilot.post("/v1/service/tenants/tenant-a/admit",json={"runtime_id":"tenant-a","operation_id":"one"})
    assert result.json()["reason"]=="expired"
    with Session(get_engine()) as session: assert session.exec(select(PilotOperation).where(PilotOperation.kind=="expired",PilotOperation.reference=="tenant-a")).one().state=="pending"


def test_founder_activation_does_not_touch_imported_credentials(pilot):
    from control_api.pilot_lifecycle import activate_model
    from control_api.pilot_models import PilotOperation
    with Session(get_engine()) as session:
        runtime_state(session)
        ent=session.get(PilotEntitlement,"tenant-a");ent.state="imported_active";ent.expires_at=None;session.add(ent);session.commit()
        clients=Mock()
        activate_model(session,PilotOperation(id="activate:tenant-a:1",account_id="tenant-a",kind="activate",reference="tenant-a"),clients)
        clients.railway.set_variables.assert_not_called()


def test_activation_reconciles_orphan_alias_and_shared_team(pilot,respx_mock):
    import json
    import httpx
    from control_api.pilot_lifecycle import activate_model
    from control_api.pilot_models import PilotOperation
    root="https://trial-proxy.squire.test"
    team_id="squire-pilot-tenant-a"
    info=respx_mock.get(root+"/team/info").mock(return_value=httpx.Response(200,json={"team_info":{"team_id":team_id,"max_budget":10.0,"budget_duration":None}}))
    revoke=respx_mock.post(root+"/key/delete").mock(return_value=httpx.Response(200,json={}))
    mint=respx_mock.post(root+"/key/generate").mock(return_value=httpx.Response(200,json={"key":"ephemeral-test-key"}))
    with Session(get_engine()) as session:
        runtime_state(session)
        tenant=session.get(Tenant,"tenant-a");tenant.railway_service_id="svc";session.add(tenant);session.commit()
        clients=Mock()
        activate_model(session,PilotOperation(id="activate:tenant-a:1",account_id="tenant-a",kind="activate",reference="tenant-a"),clients)
        assert revoke.called  # Even if the alias was never committed after an earlier crash.
        payload=json.loads(mint.calls[0].request.content)
        assert payload["team_id"]==team_id
        assert "budget_duration" not in payload
        assert payload["duration"].endswith("s")
        assert session.get(PilotRuntime,"tenant-a").deployment_generation==1
        assert session.get(PilotRuntime,"tenant-a").ready is False
        assert clients.railway.set_variables.call_args.args[1]["SQUIRE_DEPLOYMENT_GENERATION"]=="1"


def test_group_revocation_queues_scoped_cleanup_and_duplicate_does_not_reopen(pilot,monkeypatch):
    from control_api.pilot_models import PilotOperation
    from control_api.pilot_lifecycle import run_lifecycle
    with Session(get_engine()) as session:
        runtime_state(session);runtime_state(session,runtime="group-a",kind="group")
        tenant=session.get(Tenant,"group-a");tenant.railway_service_id="group-service";tenant.trial_key_active=True;tenant.trial_key_alias="group-key";session.add(tenant);session.commit()
    pilot.headers["authorization"]="Bearer messaging-only"
    body={"tenant_id":"tenant-a","conversation_id":"scope-group-a","state":"paused","binding_generation":2}
    assert pilot.post("/v1/service/groups/state",json=body).status_code==200
    clients=Mock();clients.trial.delete_trial_key.return_value=True
    monkeypatch.setattr("control_api.provisioning.ProvisionClients.build",lambda:clients)
    run_lifecycle()
    assert {call.args[0] for call in clients.trial.delete_trial_key.call_args_list}=={"group-key","squire-pilot-group-a"}
    clients.railway.stop_service.assert_called_once_with("group-service")
    assert pilot.post("/v1/service/groups/state",json=body).json()["duplicate"]
    with Session(get_engine()) as session:
        assert session.get(PilotRuntime,"group-a").state=="paused"
        assert session.get(PilotRuntime,"tenant-a").state!="paused"
        assert not session.get(Tenant,"group-a").trial_key_active
    assert pilot.post("/v1/service/groups/activate",json={"tenant_id":"tenant-a","conversation_id":"scope-group-a","claim_id":"fresh","binding_generation":3}).status_code==200
    with Session(get_engine()) as session:assert session.get(PilotRuntime,"group-a").state=="provisioning"


def test_paused_group_allows_fresh_owner_claim(pilot,monkeypatch):
    aid=account_client(pilot)
    with Session(get_engine()) as session:
        runtime_state(session);runtime_state(session,runtime="group-a",kind="group")
        account=session.get(PilotAccount,aid);account.tenant_id="tenant-a";session.add(account)
        group=session.get(PilotRuntime,"group-a");group.state="paused";session.add(group);session.commit()
    monkeypatch.setattr("control_api.routers.pilot.MessagingClient.request",lambda *a,**kw:{"claim_id":"fresh","nonce":"new"})
    assert pilot.post("/v1/groups/claims",json={}).status_code==200


def test_inbound_nonce_verified_status_finishes_signup(pilot,monkeypatch):
    aid=account_client(pilot);token=invitation("customer@example.com")
    tid=pilot.post("/v1/trial",json=profile(token)).json()["tenant_id"]
    monkeypatch.setattr("control_api.routers.pilot.MessagingClient.request",lambda *a,**kw:{"tenant_id":tid,"verified":True,"conversation_id":"verified-through-inbound-nonce"})
    assert pilot.post("/v1/phone/status",json={}).json()["verified"]
    with Session(get_engine()) as session:
        assert session.get(PilotAdmission,aid).state=="accepted"
        assert session.get(PilotRuntime,tid).scope_id=="verified-through-inbound-nonce"


def test_imported_founder_preserves_model_key_and_endpoint(pilot):
    from control_api.pilot_lifecycle import set_runtime_variables
    with Session(get_engine()) as session:
        runtime_state(session)
        ent=session.get(PilotEntitlement,"tenant-a");ent.state="imported_active";ent.expires_at=None;session.add(ent)
        tenant=session.get(Tenant,"tenant-a");tenant.railway_service_id="founder";tenant.dek_set=True;session.add(tenant);session.commit()
        clients=Mock();clients.railway.get_variable_names.return_value=["SQUIRE_DEK"];clients.railway.ensure_runtime_bucket.return_value="backup-runtime"
        set_runtime_variables(session,tenant,clients)
        variables=clients.railway.set_variables.call_args.args[1]
        assert "ANTHROPIC_API_KEY" not in variables
        assert "ANTHROPIC_BASE_URL" not in variables


def test_revoking_runtime_cannot_heartbeat_or_activate(pilot):
    from control_api.pilot_lifecycle import activate_model
    from control_api.pilot_models import PilotOperation
    with Session(get_engine()) as session:
        runtime_state(session);runtime_state(session,runtime="group-a",kind="group")
        runtime=session.get(PilotRuntime,"group-a");runtime.state="revoking";session.add(runtime);session.commit()
        clients=Mock()
        activate_model(session,PilotOperation(id="activate:group-a:1",account_id="tenant-a",kind="activate",reference="group-a"),clients)
        clients.railway.set_variables.assert_not_called();clients.railway.deploy.assert_not_called()
    pilot.headers["authorization"]="Bearer "+runtime_token("group-a","control")
    assert pilot.post("/v1/runtime/group-a/heartbeat",json={"ready":True,"model_ready":True,"deployment_generation":0}).status_code==403


def test_cleanup_revokes_uncommitted_activation_alias(pilot):
    from unittest.mock import Mock
    from control_api.pilot_lifecycle import revoke_runtime_key
    from control_api.pilot_models import PilotRuntime,PilotEntitlement,PilotOperation
    from control_api.models import Tenant
    with Session(get_engine()) as session:
        tenant=Tenant(id="orphan-key",email="orphan@example.com")
        runtime=PilotRuntime(id=tenant.id,owner_tenant_id=tenant.id,scope_id="orphan-conversation",kind="dm")
        session.add(tenant);session.add(runtime)
        session.add(PilotEntitlement(tenant_id=tenant.id,state="paused"))
        session.add(PilotOperation(id="activate:orphan-key:1",account_id=tenant.id,kind="activate",reference=tenant.id))
        session.commit()
        clients=Mock();clients.trial.delete_trial_key.return_value=True
        revoke_runtime_key(session,runtime,tenant,clients)
        clients.trial.delete_trial_key.assert_called_once_with("squire-pilot-orphan-key")


def test_extension_operator_auth_idempotency_and_audit(pilot):
    from control_api.pilot_models import PilotExtension
    with Session(get_engine()) as session:
        session.add(Tenant(id="extend-me",email="extend-me@example.com"));session.flush()
        session.add(PilotEntitlement(tenant_id="extend-me",state="expired",activated_at=utcnow()-timedelta(days=4),expires_at=utcnow()-timedelta(days=1)));session.commit()
    request={"operation_id":"operator-grant-one","operator_label":"pilot-operator","reason_code":"pilot_evaluation","expires_at":(utcnow()+timedelta(days=2)).isoformat(),"total_budget_cents":1500}
    path="/v1/operator/tenants/extend-me/extension"
    assert pilot.post(path,json=request).status_code==401
    headers={"Authorization":"Bearer test-internal-token"}
    first=pilot.post(path,json=request,headers=headers);assert first.status_code==200,first.text
    assert pilot.post(path,json=request,headers=headers).json()["id"]==first.json()["id"]
    assert pilot.post(path,json={**request,"total_budget_cents":2000},headers=headers).status_code==409
    with Session(get_engine()) as session:
        assert len(session.exec(select(PilotExtension)).all())==1
        assert session.get(PilotEntitlement,"extend-me").state=="extending"
