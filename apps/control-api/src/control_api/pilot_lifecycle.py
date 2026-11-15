"""Pilot runtime provisioning, activation and durable lifecycle reconciliation."""
from __future__ import annotations
import secrets
from datetime import timedelta
import httpx
from fastapi import HTTPException
from sqlalchemy import update, text
from sqlmodel import Session, select
from control_api import crypto
from control_api.config import get_settings
from control_api.db import get_engine
from control_api.models import Tenant, utcnow
from control_api.pilot_models import PilotRuntime, PilotEntitlement, PilotTurn, PilotOperation, PilotAccount, PilotAdmission, PilotExtension
from control_api.pilot_security import runtime_token
from control_api.clients.messaging import MessagingClient
from control_api.provisioning import as_aware


def set_runtime_variables(session,tenant,clients):
    runtime=session.get(PilotRuntime,tenant.id);s=get_settings()
    variables={"TENANT_ID":runtime.id,"SQUIRE_RUNTIME_ID":runtime.id,"SQUIRE_DEPLOYMENT_GENERATION":str(runtime.deployment_generation),"SQUIRE_RUNTIME_KIND":runtime.kind,"SQUIRE_OWNER_TENANT_ID":runtime.owner_tenant_id,"SQUIRE_CONVERSATION_ID":runtime.scope_id,"SQUIRE_MESSAGING_URL":s.messaging_base_url,"SQUIRE_EGRESS_TOKEN":runtime_token(runtime.id,"egress",runtime.generation),"SQUIRE_INGRESS_TOKEN":runtime_token(runtime.id,"ingress",runtime.generation),"SQUIRE_CONTROL_TOKEN":runtime_token(runtime.id,"control",runtime.generation),"CONTROL_API_URL":s.effective_control_api_url,"SQUIRE_IMAGE_REF":tenant.image_ref or s.tenant_image,"SQUIRE_HEARTBEAT_INTERVAL":str(s.tenant_heartbeat_interval_seconds),"SQUIRE_VOLUME":s.tenant_volume_mount_path,"PORT":str(s.tenant_port),"ANTHROPIC_BASE_URL":s.effective_trial_base_url,"SQUIRE_DISABLE_PRIVATE_TOOLS":"true" if runtime.kind=="group" else "false"}
    ent=session.get(PilotEntitlement,runtime.owner_tenant_id)
    private_provider=runtime.kind=="dm" and (tenant.connected_provider or (ent and ent.state=="imported_active"))
    if private_provider:
        variables.pop("ANTHROPIC_BASE_URL",None)
    if not tenant.trial_key_active and not private_provider:
        variables["ANTHROPIC_API_KEY"]=""
    if ent and ent.state=="imported_active" and runtime.kind=="dm" and s.pilot_founder_backup_bucket:
        bucket=s.pilot_founder_backup_bucket
        if not clients.railway.find_bucket_by_name(bucket): raise RuntimeError("founder backup bucket not found")
    else:
        bucket=clients.railway.ensure_runtime_bucket(runtime.id)
    def reference(key): return "${{"+bucket+"."+key+"}}"
    variables.update({"RESTIC_REPOSITORY":"s3:"+reference("ENDPOINT")+"/"+reference("BUCKET"),
        "AWS_ACCESS_KEY_ID":reference("ACCESS_KEY_ID"),"AWS_SECRET_ACCESS_KEY":reference("SECRET_ACCESS_KEY"),
        "AWS_DEFAULT_REGION":reference("REGION"),"SQUIRE_BACKUP_KEEP_WITHIN":"30d"})
    names=clients.railway.get_variable_names(tenant.railway_service_id)
    # Probe before generating: a crash after upsert must never rotate an existing DEK.
    if "SQUIRE_DEK" not in names:
        if tenant.dek_set: raise RuntimeError("runtime encryption variable missing")
        variables["SQUIRE_DEK"]=crypto.generate_dek()
    from control_api.provisioning import register_step_secret
    for key,value in variables.items():
        if key.endswith("TOKEN") or key=="SQUIRE_DEK": register_step_secret(value)
    clients.railway.set_variables(tenant.railway_service_id,variables)
    if "SQUIRE_DEK" not in clients.railway.get_variable_names(tenant.railway_service_id): raise RuntimeError("runtime encryption variable missing after write")
    tenant.dek_set=True;session.add(tenant);session.commit()


def register_runtime(session,tenant,clients):
    runtime=session.get(PilotRuntime,tenant.id)
    MessagingClient().request("POST","/v1/control/runtimes",{"runtime_id":runtime.id,"tenant_id":runtime.owner_tenant_id,"conversation_id":runtime.scope_id,"kind":runtime.kind,"endpoint":tenant.internal_url.rstrip("/")+"/events","ingress_token":runtime_token(runtime.id,"ingress",runtime.generation),"egress_token":runtime_token(runtime.id,"egress",runtime.generation)})
    runtime.registered=True;tenant.webhook_set=True;session.add(runtime);session.add(tenant);session.commit()


def admit_turn(session:Session,tenant_id:str,runtime_id:str,operation_id:str):
    # Serialize all DM + group consumption against the sponsor's entitlement row.
    session.execute(update(PilotEntitlement).where(PilotEntitlement.tenant_id==tenant_id).values(revision=PilotEntitlement.revision+1))
    ent=session.get(PilotEntitlement,tenant_id);runtime=session.get(PilotRuntime,runtime_id)
    now=utcnow();s=get_settings()
    def decision(allowed,reason):
        result={"allowed":allowed,"reason":reason,"activated_at":ent.activated_at if ent else None,"expires_at":ent.expires_at if ent else None,"remaining_turns":max(0,s.pilot_daily_turn_limit-ent.daily_turns) if ent else 0,"runtime_ready":bool(runtime and runtime.ready)}
        session.commit();return result
    if not ent or not runtime or runtime.owner_tenant_id!=tenant_id: return decision(False,"unknown_runtime")
    if ent.state not in {"ready_unstarted","activating","active","imported_active"}: return decision(False,ent.state)
    if ent.expires_at and as_aware(ent.expires_at)<=now:
        ent.state="expired";session.add(ent)
        operation=f"expire:{tenant_id}:{int(as_aware(ent.expires_at).timestamp())}"
        if not session.get(PilotOperation,operation): session.add(PilotOperation(id=operation,account_id=tenant_id,kind="expired",reference=tenant_id))
        return decision(False,"expired")
    if runtime.state in {"paused","expired","deleted","revoking"}: return decision(False,runtime.state)
    if not runtime.ready or not runtime.registered: return decision(False,"runtime_unavailable")
    if ent.state=="ready_unstarted":
        ent.activated_at=now;ent.expires_at=now+timedelta(hours=s.pilot_trial_hours);ent.state="activating";session.add(ent)
    tenant=session.get(Tenant,runtime.id)
    if runtime.kind=="dm" and (ent.state=="imported_active" or tenant.connected_provider) and not runtime.model_ready: return decision(False,"runtime_unavailable")
    if ent.state=="activating" or not runtime.model_ready:
        operation=f"activate:{runtime.id}:{runtime.scope_generation}"
        if not session.get(PilotOperation,operation): session.add(PilotOperation(id=operation,account_id=tenant_id,kind="activate",reference=runtime.id))
        return decision(False,"activating")
    turn_id=tenant_id+":"+operation_id
    previous=session.get(PilotTurn,turn_id)
    if previous: return decision(previous.runtime_id==runtime_id,"already_admitted" if previous.runtime_id==runtime_id else "operation_scope_mismatch")
    day=int((now-as_aware(ent.activated_at)).total_seconds()//86400)
    if day!=ent.daily_index: ent.daily_index=day;ent.daily_turns=0
    if ent.state!="imported_active" and ent.daily_turns>=s.pilot_daily_turn_limit: return decision(False,"daily_limit")
    ent.daily_turns+=1;ent.lifetime_turns+=1;session.add(ent)
    session.add(PilotTurn(id=turn_id,tenant_id=tenant_id,runtime_id=runtime_id))
    return decision(True,"admitted")


def activate_model(session,operation,clients):
    """Keys share a non-resetting team budget; retries cannot replenish spend."""
    runtime=session.get(PilotRuntime,operation.reference);tenant=session.get(Tenant,runtime.id)
    ent=session.get(PilotEntitlement,runtime.owner_tenant_id);s=get_settings()
    if not ent or ent.state not in {"activating","active","imported_active"} or (ent.expires_at and as_aware(ent.expires_at)<=utcnow()): return
    if runtime.kind=="dm" and (ent.state=="imported_active" or tenant.connected_provider): return
    expected_operation=f"activate:{runtime.id}:{runtime.scope_generation}"
    if operation.id!=expected_operation or runtime.state not in {"ready","provisioning"}: return
    if not s.litellm_base_url or not s.litellm_master_key: raise RuntimeError("trial model proxy not configured")
    team_id="squire-pilot-"+ent.tenant_id
    headers={"Authorization":"Bearer "+s.litellm_master_key}
    with httpx.Client(base_url=s.litellm_base_url.rstrip("/"),headers=headers,timeout=20) as client:
        team=client.get("/team/info",params={"team_id":team_id})
        if team.status_code==404:
            response=client.post("/team/new",json={"team_id":team_id,"team_alias":team_id,"max_budget":ent.model_budget_cents/100})
            if response.status_code>=400:
                existing=client.get("/team/info",params={"team_id":team_id})
                if existing.status_code>=400: raise RuntimeError("trial budget could not be created")
        elif team.status_code>=400: raise RuntimeError("trial budget could not be confirmed")
        confirmed=client.get("/team/info",params={"team_id":team_id})
        if confirmed.status_code>=400: raise RuntimeError("trial budget confirmation failed")
        info=confirmed.json().get("team_info",{})
        if info.get("team_id")!=team_id or info.get("max_budget")!=ent.model_budget_cents/100 or info.get("budget_duration"):
            raise RuntimeError("trial budget policy mismatch")
        session.refresh(runtime);session.refresh(ent)
        if operation.id!=f"activate:{runtime.id}:{runtime.scope_generation}" or runtime.state not in {"ready","provisioning"} or ent.state not in {"active","activating","imported_active"}:return
        alias="squire-pilot-"+runtime.id
        # Revocation before replacement is mandatory; never swallow proxy errors.
        response=client.post("/key/delete",json={"key_aliases":[alias]})
        if response.status_code>=400 and response.status_code!=404: raise RuntimeError("previous trial key could not be revoked")
        payload={"team_id":team_id,"key_alias":alias,"max_budget":ent.model_budget_cents/100,"max_parallel_requests":1,"rpm_limit":s.trial_rpm_limit,"metadata":{"tenant_id":ent.tenant_id,"runtime_id":runtime.id}}
        if ent.expires_at:
            remaining=max(1,int((as_aware(ent.expires_at)-utcnow()).total_seconds()))
            payload["duration"]=f"{remaining}s"
        response=client.post("/key/generate",json=payload)
        if response.status_code>=400 or not response.json().get("key"): raise RuntimeError("trial key could not be issued")
        key=response.json()["key"]
        # Persist alias before delivering; a crash will revoke and reissue, within the same team.
        tenant.trial_key_alias=alias;tenant.trial_key_active=True;session.add(tenant);session.commit()
        runtime.deployment_generation+=1;runtime.model_ready=False;runtime.ready=False;session.add(runtime);session.commit()
        clients.railway.set_variables(tenant.railway_service_id,{"ANTHROPIC_API_KEY":key,"ANTHROPIC_BASE_URL":s.effective_trial_base_url,"SQUIRE_TRIAL_EXPIRES_AT":as_aware(ent.expires_at).isoformat() if ent.expires_at else "","SQUIRE_DEPLOYMENT_GENERATION":str(runtime.deployment_generation)})
        session.refresh(runtime);session.refresh(ent)
        if operation.id!=f"activate:{runtime.id}:{runtime.scope_generation}" or runtime.state not in {"ready","provisioning"} or ent.state not in {"active","activating","imported_active"}:return
        clients.railway.deploy(tenant.railway_service_id)
        session.execute(update(PilotEntitlement).where(PilotEntitlement.tenant_id==ent.tenant_id,PilotEntitlement.state=="activating").values(state="active"));session.commit()


def revoke_runtime_key(session, runtime, tenant, clients):
    """Revoke even a key minted immediately before the issuing worker crashed."""
    ent=session.get(PilotEntitlement,runtime.owner_tenant_id)
    aliases=set()
    if tenant.trial_key_alias: aliases.add(tenant.trial_key_alias)
    if not (ent and ent.state=="imported_active" and runtime.kind=="dm"):
        # Founder DM is never assigned this alias; it uses its preserved provider.
        # Once a founder is paused the entitlement state changes, but its absent
        # alias plus absent activation operation still distinguishes that path.
        activation=session.exec(select(PilotOperation.id).where(PilotOperation.kind=="activate",PilotOperation.reference==runtime.id)).first()
        if runtime.kind=="group" or activation: aliases.add("squire-pilot-"+runtime.id)
    for alias in sorted(aliases):
        if not clients.trial.delete_trial_key(alias): raise RuntimeError("runtime key revocation not confirmed")
    tenant.trial_key_active=False;session.add(tenant);session.commit()


def reconcile_deadlines(session,now):
    candidates=session.exec(select(PilotEntitlement).where(PilotEntitlement.state.in_(["active","activating","ready_unstarted","setup_paused","setup_resuming"]))).all()
    for candidate in candidates:
        conditions=[PilotEntitlement.tenant_id==candidate.tenant_id,PilotEntitlement.state==candidate.state]
        target=None;kind=None
        if candidate.state in {"active","activating"} and candidate.expires_at and as_aware(candidate.expires_at)<=now:
            conditions.append(PilotEntitlement.expires_at<=now)
            target=kind="expired";oid=f"expire:{candidate.tenant_id}:{int(as_aware(candidate.expires_at).timestamp())}"
        elif candidate.activated_at is None:
            conditions.append(PilotEntitlement.activated_at.is_(None))
            if as_aware(candidate.created_at)+timedelta(days=7)<=now:
                target="deleted";kind="abandoned";oid="abandoned:"+candidate.tenant_id
            elif candidate.state=="ready_unstarted":
                resumed=session.exec(select(PilotOperation).where(PilotOperation.reference==candidate.tenant_id,PilotOperation.kind=="setup_resume").order_by(PilotOperation.created_at.desc())).first()
                idle_since=max(as_aware(candidate.created_at),as_aware(resumed.created_at)) if resumed else as_aware(candidate.created_at)
                if idle_since+timedelta(hours=24)<=now:
                    target=kind="setup_paused";oid="setup-pause:"+candidate.tenant_id+":"+str(int(idle_since.timestamp()))
        if target:
            changed=session.execute(update(PilotEntitlement).where(*conditions).values(state=target))
            if changed.rowcount and not session.get(PilotOperation,oid):session.add(PilotOperation(id=oid,account_id=candidate.tenant_id,kind=kind,reference=candidate.tenant_id))
    session.commit()


def queue_runtime_restart(session,tenant_id):
    from control_api.models import ProvisionJob,ProvisionStep,TenantStatus
    for runtime in session.exec(select(PilotRuntime).where(PilotRuntime.owner_tenant_id==tenant_id)):
        # A separately revoked group is not revived by a sponsor extension.
        if runtime.state in {"deleted","revoking"}:continue
        if runtime.kind=="group" and runtime.state=="paused" and session.get(PilotOperation,f"group-paused:{runtime.id}:{runtime.scope_generation}"):continue
        runtime.state="provisioning";runtime.ready=False;runtime.model_ready=False;runtime.registered=False
        runtime.scope_generation+=1;runtime.generation+=1;runtime.deployment_generation+=1
        tenant=session.get(Tenant,runtime.id);tenant.status=TenantStatus.PROVISIONING
        session.add(runtime);session.add(tenant)
        session.add(ProvisionJob(id=secrets.token_hex(12),tenant_id=runtime.id,step=ProvisionStep.SET_VARIABLES))


def resume_runtimes(session,op,clients):
    ent=session.get(PilotEntitlement,op.reference)
    if not ent or ent.state!="setup_resuming" or ent.activated_at:return
    if as_aware(ent.created_at)+timedelta(days=7)<=utcnow():
        reconcile_deadlines(session,utcnow());return
    MessagingClient().request("POST",f"/v1/control/tenants/{ent.tenant_id}/state",{"state":"active"})
    session.refresh(ent)
    if ent.state!="setup_resuming":return
    ent.state="ready_unstarted";session.add(ent);queue_runtime_restart(session,ent.tenant_id);session.commit()


def apply_extension(session,op,clients):
    audit=session.get(PilotExtension,op.id);ent=session.get(PilotEntitlement,op.reference);s=get_settings()
    if not audit or not ent:return
    if ent.state!="extending":
        if audit.state!="applied":audit.state="superseded";session.add(audit);session.commit()
        return
    if as_aware(audit.expires_at)<=utcnow():
        ent.state="expired";audit.state="expired";session.add(ent);session.add(audit)
        key="expire-extension:"+audit.id
        if not session.get(PilotOperation,key):session.add(PilotOperation(id=key,account_id=ent.tenant_id,kind="expired",reference=ent.tenant_id))
        session.commit();return
    from control_api.provisioning import stop_tenant
    # Halt existing work before a dependency outage can leave an extension in
    # limbo. The shared sponsor lock prevents provisioning from racing this stop.
    for runtime in session.exec(select(PilotRuntime).where(PilotRuntime.owner_tenant_id==ent.tenant_id)):
        if runtime.state!="deleted":stop_tenant(session,runtime.id,clients)
    # Budget changes modify the existing team's absolute cap. Never recreate a
    # team or write spend/budget_duration: accrued usage survives every retry.
    team_id="squire-pilot-"+ent.tenant_id
    with httpx.Client(base_url=s.litellm_base_url.rstrip("/"),headers={"Authorization":"Bearer "+s.litellm_master_key},timeout=20) as client:
        before=client.get("/team/info",params={"team_id":team_id})
        if before.status_code>=400:raise RuntimeError("existing trial budget unavailable")
        info=before.json().get("team_info",{})
        if info.get("team_id")!=team_id or info.get("budget_duration") or info.get("max_budget") not in {audit.previous_budget_cents/100,audit.budget_cents/100}:raise RuntimeError("trial budget policy mismatch")
        changed=client.post("/team/update",json={"team_id":team_id,"max_budget":audit.budget_cents/100})
        if changed.status_code>=400:raise RuntimeError("trial budget change failed")
        after=client.get("/team/info",params={"team_id":team_id})
        confirmed=after.json().get("team_info",{}) if after.status_code<400 else {}
        if confirmed.get("max_budget")!=audit.budget_cents/100 or confirmed.get("budget_duration") or float(confirmed.get("spend",0))<float(info.get("spend",0)):raise RuntimeError("trial budget change could not be confirmed")
    for runtime in session.exec(select(PilotRuntime).where(PilotRuntime.owner_tenant_id==ent.tenant_id)):
        revoke_runtime_key(session,runtime,session.get(Tenant,runtime.id),clients)
    session.refresh(ent)
    if ent.state!="extending":
        audit.state="superseded";session.add(audit);session.commit();return
    MessagingClient().request("POST",f"/v1/control/tenants/{ent.tenant_id}/state",{"state":"active","expires_at":as_aware(audit.expires_at).timestamp()})
    session.refresh(ent)
    if ent.state!="extending":
        audit.state="superseded";session.add(audit);session.commit();return
    ent.expires_at=audit.expires_at;ent.model_budget_cents=audit.budget_cents;ent.state="active";ent.extension_requested=False
    audit.state="applied";audit.applied_at=utcnow();session.add(ent);session.add(audit)
    queue_runtime_restart(session,ent.tenant_id);session.commit()


def run_lifecycle():
    from control_api.provisioning import ProvisionClients, stop_tenant, delete_tenant
    with get_engine().connect() as connection, Session(bind=connection) as session:
        now=utcnow()
        reconcile_deadlines(session,now)
        ids=session.exec(select(PilotOperation.id).where(PilotOperation.state.in_(["pending","running"]),PilotOperation.next_attempt_at<=utcnow())).all()
        for oid in ids:
            # A connection-held advisory lock survives session commits and prevents
            # a stale lease reclaim from overlapping any external side effect.
            candidate=session.get(PilotOperation,oid)
            owner=candidate.account_id if candidate.kind in {"activate","group_paused","provider_connected"} else candidate.reference
            lock_id="tenant:"+owner
            is_pg=connection.dialect.name=="postgresql"
            if is_pg:
                acquired=session.execute(text("SELECT pg_try_advisory_lock(hashtextextended(:id,0))"),{"id":lock_id}).scalar_one()
                session.commit()
                if not acquired: continue
            try:
                claimed=session.execute(update(PilotOperation).where(PilotOperation.id==oid,((PilotOperation.state=="pending") | (PilotOperation.lease_until<now))).values(state="running",lease_until=now+timedelta(minutes=10),lease_generation=PilotOperation.lease_generation+1))
                session.commit()
                if not claimed.rowcount: continue
                op=session.get(PilotOperation,oid);generation=op.lease_generation
                try:
                    clients=ProvisionClients.build()
                    if op.kind=="activate": activate_model(session,op,clients)
                    elif op.kind=="extension": apply_extension(session,op,clients)
                    elif op.kind=="setup_resume": resume_runtimes(session,op,clients)
                    elif op.kind=="provider_connected":
                        tenant=session.get(Tenant,op.reference)
                        runtime=session.get(PilotRuntime,tenant.id)
                        revoke_runtime_key(session,runtime,tenant,clients)
                    elif op.kind=="group_paused":
                        runtime=session.get(PilotRuntime,op.reference);tenant=session.get(Tenant,runtime.id)
                        stop_tenant(session,tenant.id,clients)
                        revoke_runtime_key(session,runtime,tenant,clients)
                        runtime.state="paused";runtime.ready=False;runtime.model_ready=False;runtime.generation+=1;session.add(runtime);session.commit()
                    else:
                        entitlement=session.get(PilotEntitlement,op.reference)
                        expected="deleted" if op.kind=="abandoned" else op.kind
                        # A newer resume/extension/pause decision supersedes older cleanup.
                        if entitlement and entitlement.state==expected:
                            external="paused" if op.kind=="setup_paused" else "deleted" if op.kind=="abandoned" else op.kind
                            MessagingClient().request("POST",f"/v1/control/tenants/{op.reference}/state",{"state":external})
                            for runtime in session.exec(select(PilotRuntime).where(PilotRuntime.owner_tenant_id==op.reference)):
                                tenant=session.get(Tenant,runtime.id)
                                stop_tenant(session,tenant.id,clients)
                                revoke_runtime_key(session,runtime,tenant,clients)
                                if external=="deleted": delete_tenant(session,tenant.id,clients)
                                runtime.state=external;runtime.ready=False;runtime.model_ready=False;runtime.generation+=1;session.add(runtime)
                            if op.kind=="abandoned":
                                account=session.exec(select(PilotAccount).where(PilotAccount.tenant_id==op.reference)).first()
                                admission=session.get(PilotAdmission,account.id) if account else None
                                if admission:admission.state="released";session.add(admission)
                            session.commit()
                    session.execute(update(PilotOperation).where(PilotOperation.id==oid,PilotOperation.lease_generation==generation).values(state="done",lease_until=None,last_code=None));session.commit()
                except Exception:
                    session.rollback()
                    session.execute(update(PilotOperation).where(PilotOperation.id==oid,PilotOperation.lease_generation==generation).values(state="pending",attempts=PilotOperation.attempts+1,next_attempt_at=utcnow()+timedelta(seconds=60),lease_until=None,last_code="dependency_unavailable"));session.commit()
            finally:
                if is_pg:
                    session.execute(text("SELECT pg_advisory_unlock(hashtextextended(:id,0))"),{"id":lock_id})
                    session.commit()
