"""Invitation-only pilot signup and capability-scoped service APIs."""
from __future__ import annotations
import hmac
import re
import secrets
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, update
from sqlmodel import Session, select
from control_api.auth import require_internal_token
from control_api.config import get_settings
from control_api.db import get_session
from control_api.models import Tenant, TenantStatus, ProvisionJob, utcnow
from control_api.provisioning import as_aware, internal_url_for, service_name_for
from control_api.pilot_models import (PilotAccount, PilotAuthChallenge, PilotSession, PilotInvitation, PilotCapacity, PilotAdmission, PilotRuntime, PilotEntitlement, PilotOperation, PilotExtension)
from control_api.pilot_security import *
from control_api.clients.messaging import MessagingClient, send_auth_email

router = APIRouter(prefix="/v1", tags=["pilot"])
CONSENT = "pilot-2026-11-15"

class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")

class Email(Strict):
    email: str = Field(min_length=3, max_length=254)
    @field_validator("email")
    @classmethod
    def valid(cls, value):
        import re
        value = value.strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("enter a valid email address")
        return value

class Login(Email):
    invitation: str = Field(default="", max_length=128)

class Verify(Strict):
    challenge_id: str = Field(max_length=64)
    code: str = Field(pattern=r"^\d{8}$")

class Profile(Strict):
    invitation: str = Field(default="", max_length=128)
    display_name: str = Field(min_length=1,max_length=80)
    timezone: str = Field(max_length=64)
    consent_version: Literal["pilot-2026-11-15"]
    @field_validator("timezone")
    @classmethod
    def valid_zone(cls, value):
        try: ZoneInfo(value)
        except ZoneInfoNotFoundError: raise ValueError("unknown timezone")
        return value

class Phone(Strict):
    operation_id: str = Field(min_length=16,max_length=100)
    phone: str = Field(pattern=r"^\+[1-9]\d{7,14}$")

class PhoneVerify(Strict):
    challenge_id: str = Field(max_length=100)
    code: str = Field(pattern=r"^\d{6,8}$")

class Admit(Strict):
    operation_id: str = Field(min_length=1,max_length=200)
    runtime_id: str = Field(max_length=100)

class GroupActivate(Strict):
    tenant_id: str = Field(max_length=100)
    conversation_id: str = Field(max_length=100)
    claim_id: str = Field(max_length=100)
    binding_generation: int = Field(default=1,ge=1)

class Beat(Strict):
    ready: bool
    model_ready: bool = False
    deployment_generation: int = Field(default=0,ge=0)

class Lifecycle(Strict):
    state: Literal["paused","deleted"]


def lock_capacity(session):
    # A write lock serializes seats on SQLite and row-locks the singleton on Postgres.
    session.execute(update(PilotCapacity).where(PilotCapacity.id=="pilot").values(revision=PilotCapacity.revision+1))


def invitation_for(session, email, token):
    if not token: return None
    inv = session.exec(select(PilotInvitation).where(PilotInvitation.digest==digest("invitation",token))).first()
    if inv and inv.email==email and inv.state=="available" and as_aware(inv.expires_at)>utcnow(): return inv
    return None

@router.post("/auth/start")
def auth_start(body: Login, request: Request, session: Session=Depends(get_session)):
    pilot_available(); check_origin(request)
    limit(session,"auth-ip:"+(request.client.host if request.client else "unknown"),200)
    limit(session,"auth-email:"+body.email,5)
    account=session.exec(select(PilotAccount).where(PilotAccount.email==body.email)).first()
    eligible = bool(account and account.state!="deleted") or bool(invitation_for(session,body.email,body.invitation))
    challenge_id=secrets.token_urlsafe(24)
    if eligible:
        code=f"{secrets.randbelow(100000000):08d}"
        row=PilotAuthChallenge(id=challenge_id,email=body.email,digest=digest("login",challenge_id+":"+code),expires_at=utcnow()+timedelta(minutes=10))
        session.add(row);session.commit()
        send_auth_email(body.email,code,challenge_id)
    return {"challenge_id":challenge_id,"status":"If this address has an invitation or account, a sign-in code has been sent."}

@router.post("/auth/verify")
def auth_verify(body: Verify, request: Request, response: Response, session: Session=Depends(get_session)):
    pilot_available(); check_origin(request)
    limit(session,"verify-ip:"+(request.client.host if request.client else "unknown"),300)
    claimed=session.execute(update(PilotAuthChallenge).where(PilotAuthChallenge.id==body.challenge_id, PilotAuthChallenge.consumed==False, PilotAuthChallenge.attempts<5, PilotAuthChallenge.expires_at>utcnow()).values(attempts=PilotAuthChallenge.attempts+1))
    if not claimed.rowcount:
        session.rollback();raise HTTPException(400,"invalid or expired code")
    row=session.get(PilotAuthChallenge,body.challenge_id)
    valid=hmac.compare_digest(row.digest,digest("login",row.id+":"+body.code))
    if valid: row.consumed=True
    session.add(row);session.commit()
    if not valid: raise HTTPException(400,"invalid or expired code")
    account=session.exec(select(PilotAccount).where(PilotAccount.email==row.email)).first()
    if not account:
        account=PilotAccount(id=secrets.token_hex(12),email=row.email); session.add(account);session.commit()
    token,csrf=issue_session(session,account.id)
    response.set_cookie(COOKIE,token,httponly=True,secure=get_settings().pilot_cookie_secure,samesite="strict",max_age=604800,path="/")
    return {"csrf_token":csrf,"account_id":account.id}

@router.post("/auth/logout")
def logout(request:Request,response:Response,account=Depends(current_account),session:Session=Depends(get_session)):
    row=session.get(PilotSession,digest("session",request.cookies[COOKIE]));row.revoked=True;session.add(row);session.commit()
    response.delete_cookie(COOKIE,path="/");return {"signed_out":True}

@router.get("/me")
def me(request:Request,account=Depends(current_account),session:Session=Depends(get_session)):
    entitlement=session.get(PilotEntitlement,account.tenant_id) if account.tenant_id else None
    runtime=session.get(PilotRuntime,account.tenant_id) if account.tenant_id else None
    admission=session.get(PilotAdmission,account.id)
    status=None
    if account.tenant_id:
        try: status=MessagingClient().request("GET",f"/v1/control/tenants/{account.tenant_id}")
        except HTTPException: status={"status":"temporarily_unavailable"}
    return {"account_id":account.id,"email":account.email,"display_name":account.display_name,"timezone":account.timezone,"tenant_id":account.tenant_id,"account_state":account.state,"csrf_token":digest("csrf-derive",request.cookies[COOKIE]),"consent_version":CONSENT,"admission_state":admission.state if admission else None,"trial_state":entitlement.state if entitlement else None,"expires_at":entitlement.expires_at if entitlement else None,"runtime_ready":bool(runtime and runtime.ready),"messaging":status,"service_whatsapp_number":get_settings().pilot_whatsapp_number,"daily_turn_limit":get_settings().pilot_daily_turn_limit,"turns_used":entitlement.daily_turns if entitlement else 0}

@router.post("/trial")
def reserve(body:Profile,account=Depends(current_account),session:Session=Depends(get_session)):
    if not get_settings().pilot_admission_open: raise HTTPException(503,"pilot admission is paused")
    lock_capacity(session)
    admission=session.get(PilotAdmission,account.id)
    previous_entitlement=session.get(PilotEntitlement,account.tenant_id) if account.tenant_id else None
    if previous_entitlement and previous_entitlement.state=="deleted":raise HTTPException(409,"this setup has ended; contact the pilot operator before reserving another place")
    if admission and (admission.state=="accepted" or as_aware(admission.reserved_until)>utcnow()):
        return {"tenant_id":account.tenant_id,"state":admission.state}
    inv=invitation_for(session,account.email,body.invitation)
    if not inv: raise HTTPException(403,"an invitation for this email is required")
    occupied=session.exec(select(func.count()).select_from(PilotAdmission).where((PilotAdmission.state=="accepted") | ((PilotAdmission.state=="reserved") & (PilotAdmission.reserved_until>utcnow())))).one()
    if occupied>=get_settings().pilot_seat_limit: raise HTTPException(409,"all pilot places are reserved")
    if not account.tenant_id:
        account.tenant_id=secrets.token_hex(12)
        session.add(Tenant(id=account.tenant_id,email=account.email,railway_service_name=service_name_for(account.tenant_id),internal_url=internal_url_for(account.tenant_id)))
    account.display_name=body.display_name.strip(); account.timezone=body.timezone;account.consent_version=body.consent_version;account.consent_at=utcnow()
    admission=admission or PilotAdmission(account_id=account.id,reserved_until=utcnow())
    admission.state="reserved";admission.invitation_id=inv.id;admission.reserved_until=utcnow()+timedelta(minutes=30)
    session.add(account);session.add(admission);session.commit()
    return {"tenant_id":account.tenant_id,"state":"reserved","reserved_until":admission.reserved_until}

@router.post("/phone/challenge")
def phone_challenge(body:Phone,account=Depends(current_account),session:Session=Depends(get_session)):
    admission=session.get(PilotAdmission,account.id)
    if not admission or (admission.state!="accepted" and as_aware(admission.reserved_until)<=utcnow()): raise HTTPException(409,"reserve a pilot place first")
    limit(session,"phone:"+account.id,5)
    return MessagingClient().request("POST","/v1/control/phone/challenges",{"operation_id":body.operation_id,"tenant_id":account.tenant_id,"account_id":account.id,"phone":body.phone,"consent_version":account.consent_version})

@router.post("/phone/verify")
def phone_verify(body:PhoneVerify,account=Depends(current_account),session:Session=Depends(get_session)):
    lock_capacity(session)
    admission=session.get(PilotAdmission,account.id)
    if not admission or (admission.state!="accepted" and as_aware(admission.reserved_until)<=utcnow()): raise HTTPException(409,"your place reservation expired; reserve again")
    result=MessagingClient().request("POST","/v1/control/phone/verify",{"account_id":account.id,**body.model_dump()})
    return finish_verification(session,account,admission,result)

def finish_verification(session,account,admission,result):
    if result.get("tenant_id")!=account.tenant_id or result.get("verified") is not True or not result.get("conversation_id"): raise HTTPException(502,"verification could not be confirmed")
    admission.state="accepted";admission.accepted_at=utcnow()
    inv=session.get(PilotInvitation,admission.invitation_id)
    if inv: inv.state="consumed";inv.account_id=account.id;session.add(inv)
    if not session.get(PilotRuntime,account.tenant_id):
        session.add(PilotRuntime(id=account.tenant_id,owner_tenant_id=account.tenant_id,scope_id=result["conversation_id"]))
        session.add(PilotEntitlement(tenant_id=account.tenant_id))
        session.add(ProvisionJob(id=secrets.token_hex(12),tenant_id=account.tenant_id))
    session.add(admission);session.commit()
    return {"verified":True,"state":"provisioning"}

@router.post("/groups/claims")
def group_claim(account=Depends(current_account),session:Session=Depends(get_session)):
    ent=session.get(PilotEntitlement,account.tenant_id)
    if not ent or ent.state not in {"active","ready_unstarted","imported_active"}: raise HTTPException(409,"your trial must be available")
    count=session.exec(select(func.count()).select_from(PilotRuntime).where(PilotRuntime.owner_tenant_id==account.tenant_id,PilotRuntime.kind=="group",PilotRuntime.state.not_in(["deleted","paused"]))).one()
    if count>=get_settings().pilot_group_limit: raise HTTPException(409,"pilot group limit reached")
    return MessagingClient().request("POST","/v1/control/groups/claims",{"tenant_id":account.tenant_id,"operation_id":secrets.token_hex(16)})

@router.post("/account/state")
def account_state(body:Lifecycle,account=Depends(current_account),session:Session=Depends(get_session)):
    ent=session.get(PilotEntitlement,account.tenant_id)
    if ent: ent.state=body.state;session.add(ent)
    account.state=body.state;session.add(account)
    if account.tenant_id and ent:session.add(PilotOperation(id=secrets.token_hex(12),account_id=account.id,kind=body.state,reference=account.tenant_id))
    session.commit()
    return {"state":body.state,"cleanup":"pending"}

@router.post("/trial/extension")
def extension(account=Depends(current_account),session:Session=Depends(get_session)):
    ent=session.get(PilotEntitlement,account.tenant_id)
    if not ent: raise HTTPException(409,"no trial")
    ent.extension_requested=True;session.add(ent);session.commit();return {"requested":True}

@router.post("/operator/invitations",dependencies=[Depends(require_internal_token)])
def invite(body:Email,session:Session=Depends(get_session)):
    pilot_available();token=secrets.token_urlsafe(32)
    row=PilotInvitation(id=secrets.token_hex(12),digest=digest("invitation",token),email=body.email,expires_at=utcnow()+timedelta(days=14))
    session.add(row);session.commit();return {"id":row.id,"signup_url":get_settings().pilot_web_origin+"/#invite="+token,"expires_at":row.expires_at}

@router.get("/service/tenants/{tenant_id}/entitlement",dependencies=[Depends(require_messaging_service)])
def entitlement(tenant_id:str,runtime_id:str|None=None,session:Session=Depends(get_session)):
    ent=session.get(PilotEntitlement,tenant_id)
    if not ent: raise HTTPException(404,"tenant not found")
    expired=bool(ent.expires_at and as_aware(ent.expires_at)<=utcnow())
    runtime=session.get(PilotRuntime,runtime_id or tenant_id)
    if runtime and runtime.owner_tenant_id!=tenant_id: raise HTTPException(403,"runtime scope mismatch")
    return {"allowed":ent.state in {"active","ready_unstarted","imported_active"} and not expired,"state":"expired" if expired else ent.state,"expires_at":ent.expires_at,"runtime_ready":bool(runtime and runtime.ready)}

@router.post("/service/tenants/{tenant_id}/admit",dependencies=[Depends(require_messaging_service)])
def admit(tenant_id:str,body:Admit,session:Session=Depends(get_session)):
    from control_api.pilot_lifecycle import admit_turn
    return admit_turn(session,tenant_id,body.runtime_id,body.operation_id)

@router.post("/service/groups/activate",dependencies=[Depends(require_messaging_service)])
def activate_group(body:GroupActivate,session:Session=Depends(get_session)):
    lock_capacity(session)
    ent=session.get(PilotEntitlement,body.tenant_id)
    if not ent or ent.state not in {"active","ready_unstarted","imported_active"}: raise HTTPException(409,"sponsor trial unavailable")
    existing=session.exec(select(PilotRuntime).where(PilotRuntime.scope_id==body.conversation_id)).first()
    if existing:
        if existing.owner_tenant_id!=body.tenant_id: raise HTTPException(409,"group already sponsored")
        if existing.state in {"paused","revoking"}:
            if existing.state!="paused" or body.binding_generation<=existing.scope_generation: raise HTTPException(409,"group cleanup or fresh approval required")
            active_groups=session.exec(select(func.count()).select_from(PilotRuntime).where(PilotRuntime.owner_tenant_id==body.tenant_id,PilotRuntime.kind=="group",PilotRuntime.state.not_in(["deleted","paused"]))).one()
            if active_groups>=get_settings().pilot_group_limit:raise HTTPException(409,"pilot group limit reached")
            existing.scope_generation=body.binding_generation;existing.state="provisioning";existing.ready=False;existing.model_ready=False
            tenant=session.get(Tenant,existing.id);tenant.status=TenantStatus.PROVISIONING;session.add(tenant);session.add(existing)
            from control_api.models import ProvisionStep
            session.add(ProvisionJob(id=secrets.token_hex(12),tenant_id=existing.id,step=ProvisionStep.SET_VARIABLES));session.commit()
        return {"runtime_id":existing.id,"state":existing.state}
    count=session.exec(select(func.count()).select_from(PilotRuntime).where(PilotRuntime.owner_tenant_id==body.tenant_id,PilotRuntime.kind=="group",PilotRuntime.state.not_in(["deleted","paused"]))).one()
    if count>=get_settings().pilot_group_limit: raise HTTPException(409,"pilot group limit reached")
    rid=secrets.token_hex(12)
    session.add(Tenant(id=rid,email=f"group.{rid}@runtime.invalid",railway_service_name=service_name_for(rid),internal_url=internal_url_for(rid)))
    session.flush();session.add(PilotRuntime(id=rid,owner_tenant_id=body.tenant_id,scope_id=body.conversation_id,kind="group",scope_generation=body.binding_generation))
    session.add(ProvisionJob(id=secrets.token_hex(12),tenant_id=rid));session.commit()
    return {"runtime_id":rid,"state":"provisioning"}

@router.post("/runtime/{runtime_id}/heartbeat")
def runtime_heartbeat(runtime_id:str,body:Beat,request:Request,session:Session=Depends(get_session)):
    runtime=require_runtime(request,session,runtime_id)
    if body.deployment_generation!=runtime.deployment_generation: raise HTTPException(409,"superseded deployment heartbeat")
    runtime.ready=body.ready;runtime.model_ready=body.model_ready;runtime.last_seen_at=utcnow()
    if body.ready: runtime.state="ready"
    session.add(runtime);session.commit();return {"accepted":True}

class FounderImport(Email):
    tenant_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{8,64}$")
    conversation_id: str = Field(min_length=1,max_length=100)
    service_id: str = Field(min_length=1,max_length=100)
    volume_id: str = Field(min_length=1,max_length=100)
    service_name: str = Field(pattern=r"^[a-z0-9-]{1,100}$")
    internal_url: str = Field(pattern=r"^http://[a-z0-9.-]+\.railway\.internal:[0-9]+$")
    display_name: str = Field(min_length=1,max_length=80)
    timezone: str = Field(default="UTC",max_length=64)
    encryption_confirmed: Literal[True]
    consent_version: Literal["pilot-2026-11-15"]

@router.post("/operator/founder",dependencies=[Depends(require_internal_token)])
def founder(body:FounderImport,session:Session=Depends(get_session)):
    """Adopt a rehearsed runtime; never reads or copies private volume contents."""
    pilot_available();lock_capacity(session)
    founder_image=get_settings().pilot_founder_image
    if not re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}",founder_image):raise HTTPException(503,"immutable founder-compatible image must be configured before import")
    existing=session.get(PilotRuntime,body.tenant_id)
    if existing:
        if existing.scope_id!=body.conversation_id: raise HTTPException(409,"founder already imported with a different scope")
        return {"tenant_id":existing.id,"state":existing.state}
    occupied=session.exec(select(func.count()).select_from(PilotAdmission).where((PilotAdmission.state=="accepted") | ((PilotAdmission.state=="reserved") & (PilotAdmission.reserved_until>utcnow())))).one()
    if occupied>=get_settings().pilot_seat_limit: raise HTTPException(409,"pilot is full")
    account=session.exec(select(PilotAccount).where(PilotAccount.email==body.email)).first()
    if account and account.tenant_id and account.tenant_id!=body.tenant_id: raise HTTPException(409,"account already bound")
    account=account or PilotAccount(id=secrets.token_hex(12),email=body.email)
    account.tenant_id=body.tenant_id;account.display_name=body.display_name;account.timezone=body.timezone;account.consent_version=body.consent_version;account.consent_at=utcnow()
    tenant=session.get(Tenant,body.tenant_id)
    if tenant is None: tenant=Tenant(id=body.tenant_id,email=body.email)
    tenant.railway_service_id=body.service_id;tenant.railway_volume_id=body.volume_id;tenant.railway_service_name=body.service_name;tenant.internal_url=body.internal_url;tenant.dek_set=True;tenant.image_ref=founder_image
    session.add(tenant);session.add(account);session.flush()
    session.add(PilotAdmission(account_id=account.id,state="accepted",reserved_until=utcnow(),accepted_at=utcnow()))
    session.add(PilotRuntime(id=tenant.id,owner_tenant_id=tenant.id,scope_id=body.conversation_id))
    session.add(PilotEntitlement(tenant_id=tenant.id,state="imported_active",activated_at=utcnow()))
    from control_api.models import ProvisionStep
    session.add(ProvisionJob(id=secrets.token_hex(12),tenant_id=tenant.id,step=ProvisionStep.SET_VARIABLES))
    session.commit();return {"tenant_id":tenant.id,"state":"provisioning","account_id":account.id}

class GroupState(Strict):
    active: bool | None = None
    mode: Literal["addressed","all"] | None = None

@router.post("/groups/{conversation_id}")
def group_state(conversation_id:str,body:GroupState,account=Depends(current_account),session:Session=Depends(get_session)):
    runtime=session.exec(select(PilotRuntime).where(PilotRuntime.scope_id==conversation_id,PilotRuntime.owner_tenant_id==account.tenant_id,PilotRuntime.kind=="group")).first()
    if not runtime: raise HTTPException(404,"group not found")
    result=MessagingClient().request("PATCH",f"/v1/control/groups/{conversation_id}",{"tenant_id":account.tenant_id,**body.model_dump(exclude_none=True)})
    if body.active is False:
        runtime.state="revoking";runtime.ready=False;session.add(runtime)
        oid=f"group-paused:{runtime.id}:{runtime.scope_generation}"
        if not session.get(PilotOperation,oid): session.add(PilotOperation(id=oid,account_id=account.tenant_id,kind="group_paused",reference=runtime.id))
        session.commit()
    return result

class ServiceState(Strict):
    state: Literal["paused","expired","deleted"]

@router.post("/service/tenants/{tenant_id}/state",dependencies=[Depends(require_messaging_service)])
def service_state(tenant_id:str,body:ServiceState,session:Session=Depends(get_session)):
    ent=session.get(PilotEntitlement,tenant_id)
    if not ent: raise HTTPException(404,"tenant not found")
    if ent.state!="deleted": ent.state=body.state;session.add(ent)
    op_id=body.state+":"+tenant_id
    if not session.get(PilotOperation,op_id): session.add(PilotOperation(id=op_id,account_id=tenant_id,kind=body.state,reference=tenant_id))
    session.commit();return {"state":ent.state}

class ApprovalDecision(Strict):
    decision: Literal["approve","deny","release"]
    result: str | None = Field(default=None,max_length=10000)

@router.get("/approvals")
def approvals(account=Depends(current_account)):
    if not account.tenant_id: return {"approvals":[]}
    return MessagingClient().request("GET",f"/v1/control/tenants/{account.tenant_id}/approvals")

@router.post("/approvals/{approval_id}")
def approval_decision(approval_id:str,body:ApprovalDecision,account=Depends(current_account)):
    if not account.tenant_id: raise HTTPException(409,"no agent")
    # Transient passthrough only: no approval text or result is persisted here.
    return MessagingClient().request("POST",f"/v1/control/approvals/{approval_id}",{"tenant_id":account.tenant_id,**body.model_dump(exclude_none=True)})

class GroupServiceState(Strict):
    tenant_id: str = Field(max_length=100)
    conversation_id: str = Field(max_length=100)
    state: Literal["paused"]
    binding_generation: int = Field(ge=1)

@router.post("/service/groups/state",dependencies=[Depends(require_messaging_service)])
def group_service_state(body:GroupServiceState,session:Session=Depends(get_session)):
    runtime=session.exec(select(PilotRuntime).where(PilotRuntime.scope_id==body.conversation_id,PilotRuntime.owner_tenant_id==body.tenant_id,PilotRuntime.kind=="group")).first()
    if not runtime: raise HTTPException(404,"group not found")
    if body.binding_generation<runtime.scope_generation:return {"state":runtime.state,"stale":True}
    oid=f"group-paused:{runtime.id}:{body.binding_generation}"
    prior=session.get(PilotOperation,oid)
    if prior and prior.state=="done":return {"state":runtime.state,"duplicate":True}
    runtime.scope_generation=body.binding_generation;runtime.state="revoking";runtime.ready=False;session.add(runtime)
    if not session.get(PilotOperation,oid):session.add(PilotOperation(id=oid,account_id=body.tenant_id,kind="group_paused",reference=runtime.id))
    session.commit();return {"state":runtime.state}

class RuntimeProvider(Strict):
    provider: Literal["openai","anthropic","chatgpt"]

@router.post("/runtime/{runtime_id}/provider")
def runtime_provider(runtime_id:str,body:RuntimeProvider,request:Request,session:Session=Depends(get_session)):
    runtime=require_runtime(request,session,runtime_id)
    if runtime.kind!="dm": raise HTTPException(403,"groups cannot connect private providers")
    tenant=session.get(Tenant,runtime.id);tenant.connected_provider=body.provider;session.add(tenant)
    oid="provider:"+runtime.id+":"+str(runtime.generation)
    if not session.get(PilotOperation,oid):session.add(PilotOperation(id=oid,account_id=runtime.owner_tenant_id,kind="provider_connected",reference=runtime.id))
    session.commit();return {"accepted":True}

@router.post("/phone/status")
def phone_status(account=Depends(current_account),session:Session=Depends(get_session)):
    lock_capacity(session)
    admission=session.get(PilotAdmission,account.id)
    if not admission or (admission.state!="accepted" and as_aware(admission.reserved_until)<=utcnow()): raise HTTPException(409,"reserve a pilot place first")
    result=MessagingClient().request("GET",f"/v1/control/tenants/{account.tenant_id}")
    if not result.get("verified"):return {"verified":False}
    return finish_verification(session,account,admission,result)


class ExtensionApproval(Strict):
    operation_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{8,100}$")
    operator_label: str = Field(min_length=1,max_length=100)
    reason_code: Literal["pilot_evaluation","service_recovery","customer_support"]
    expires_at: datetime
    total_budget_cents: int = Field(ge=1000,le=100000)

@router.post("/operator/tenants/{tenant_id}/extension",dependencies=[Depends(require_internal_token)])
def approve_extension(tenant_id:str,body:ExtensionApproval,session:Session=Depends(get_session)):
    if body.expires_at.tzinfo is None: raise HTTPException(422,"extension deadline must be timezone-aware")
    session.execute(update(PilotEntitlement).where(PilotEntitlement.tenant_id==tenant_id).values(revision=PilotEntitlement.revision+1))
    ent=session.get(PilotEntitlement,tenant_id)
    key="extension:"+tenant_id+":"+body.operation_id
    previous=session.get(PilotExtension,key)
    if previous:
        if (previous.operator_label,previous.reason_code,as_aware(previous.expires_at),previous.budget_cents)!=(body.operator_label,body.reason_code,body.expires_at,body.total_budget_cents):raise HTTPException(409,"operation identity reused")
        session.commit();return {"id":key,"state":previous.state}
    if not utcnow()<body.expires_at<=utcnow()+timedelta(days=30):raise HTTPException(422,"extension deadline must be within 30 days")
    if not ent or not ent.activated_at or ent.state not in {"active","expired"}:raise HTTPException(409,"only an active or expired customer trial can be extended")
    if body.total_budget_cents<ent.model_budget_cents or (ent.expires_at and body.expires_at<as_aware(ent.expires_at)):raise HTTPException(422,"extension cannot shorten time or lower the existing total budget")
    session.add(PilotExtension(id=key,tenant_id=tenant_id,operator_label=body.operator_label,reason_code=body.reason_code,previous_expires_at=ent.expires_at,expires_at=body.expires_at,previous_budget_cents=ent.model_budget_cents,budget_cents=body.total_budget_cents))
    ent.state="extending";session.add(ent)
    session.add(PilotOperation(id=key,account_id=tenant_id,kind="extension",reference=tenant_id));session.commit()
    return {"id":key,"state":"approved","cleanup":"pending"}

@router.get("/operator/tenants/{tenant_id}/extensions",dependencies=[Depends(require_internal_token)])
def extension_history(tenant_id:str,session:Session=Depends(get_session)):
    return {"extensions":session.exec(select(PilotExtension).where(PilotExtension.tenant_id==tenant_id).order_by(PilotExtension.created_at)).all()}

@router.post("/trial/resume")
def resume_setup(account=Depends(current_account),session:Session=Depends(get_session)):
    session.execute(update(PilotEntitlement).where(PilotEntitlement.tenant_id==account.tenant_id).values(revision=PilotEntitlement.revision+1))
    ent=session.get(PilotEntitlement,account.tenant_id)
    if not ent or ent.state!="setup_paused" or ent.activated_at or as_aware(ent.created_at)+timedelta(days=7)<=utcnow():raise HTTPException(409,"setup is not resumable")
    ent.state="setup_resuming";session.add(ent)
    oid="setup-resume:"+account.tenant_id+":"+secrets.token_hex(8)
    session.add(PilotOperation(id=oid,account_id=account.tenant_id,kind="setup_resume",reference=account.tenant_id));session.commit()
    return {"state":"setup_resuming"}
