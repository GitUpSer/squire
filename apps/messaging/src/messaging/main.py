"""Private messaging API. Network location is not an authorization boundary."""
from contextlib import asynccontextmanager
import hmac
import time
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select
from .config import settings
from .crypto import digest, identifier, open_sealed, seal
from .db import engine, migrate, verify_schema
from .groups import revoke_group
from .ingest import accept_batch
from .models import (Binding, Checkpoint, InboxEvent, Outbox, ProviderAccount,
                     Runtime, ShareApproval, TenantState)
from .schemas import (ChallengeRequest, ClaimRequest, GroupSettings, ImportBinding,
    IngestBatch, OutboundRequest, ProviderControl, RuntimeRegistration, ShareDecision,
    ShareRequest, StateRequest, VerifyChallenge)
from .service import (create_challenge, create_group_claim, eligible, enqueue_runtime,
    import_binding, lock_tenant, now, queue_text, register_runtime,
    set_tenant_state, tenant_status, verify_challenge)


@asynccontextmanager
async def lifespan(app):
    settings().check_secrets()
    if settings().auto_migrate:
        migrate()
    verify_schema()
    yield


app = FastAPI(title="Squire private messaging", docs_url=None, redoc_url=None,
              openapi_url=None, lifespan=lifespan)


@app.middleware("http")
async def bounded_request(request: Request, call_next):
    from fastapi.responses import JSONResponse
    # Broker batches are bounded to 100 x 32 KiB, plus metadata.
    limit = 14_150_000 if request.url.path == "/v1/outbound" else 4 * 1024 * 1024
    try:
        length = int(request.headers.get("content-length", "0"))
    except ValueError:
        return JSONResponse({"detail": "invalid content length"}, status_code=400)
    if length > limit:
        return JSONResponse({"detail": "request too large"}, status_code=413)
    size, chunks = 0, []
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return JSONResponse({"detail": "request too large"}, status_code=413)
        chunks.append(chunk)
    request._body = b"".join(chunks)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


def session():
    # All callers use dependency scope="function": commit must finish before
    # HTTP acknowledgement, not during FastAPI's post-response request teardown.
    with Session(engine()) as db:
        try:
            yield db
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "concurrent update; retry same operation") from None
        except Exception:
            db.rollback()
            raise


def bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "authentication required")
    return authorization[7:]


def principal(role):
    def check(authorization: str | None = Header(default=None)):
        configured = getattr(settings(), role + "_token")
        if not configured or not hmac.compare_digest(bearer(authorization), configured):
            raise HTTPException(401, "invalid credential")
    return check


control = principal("control")
transport = principal("transport")
operator = principal("operator")


def authenticated_runtime(authorization: str | None = Header(default=None), db: Session = Depends(session, scope="function")):
    hashed = digest("runtime-token", bearer(authorization))
    runtime = db.exec(select(Runtime).where(Runtime.egress_token_hash == hashed, Runtime.active == True)).first()
    if not runtime:
        raise HTTPException(401, "invalid runtime credential")
    return runtime


@app.get("/healthz")
def health():
    return {"status": "ok"}


@app.get("/v1/media/{event_id}")
def attachment(event_id: str, runtime: Runtime = Depends(authenticated_runtime), db: Session = Depends(session, scope="function")):
    from .media import fetch_media
    return fetch_media(db, runtime, event_id)


@app.get("/readyz")
def ready(db: Session = Depends(session, scope="function")):
    account = db.get(ProviderAccount, settings().provider_account_id)
    if account is None:
        raise HTTPException(503, "schema not ready")
    return {"ready": True}


@app.post("/v1/ingest/batches", dependencies=[Depends(transport)])
def ingest(req: IngestBatch, db: Session = Depends(session, scope="function")):
    return accept_batch(db, req)


@app.get("/v1/ingest/checkpoints/{store_id}", dependencies=[Depends(transport)])
def checkpoint(store_id: str, db: Session = Depends(session, scope="function")):
    row = db.get(Checkpoint, store_id)
    return {"store_id": store_id, "checkpoint": row.checkpoint if row else None}


@app.post("/v1/control/phone/challenges", dependencies=[Depends(control)])
def challenge(req: ChallengeRequest, db: Session = Depends(session, scope="function")):
    return create_challenge(db, req)


@app.post("/v1/control/phone/verify", dependencies=[Depends(control)])
def verify(req: VerifyChallenge, db: Session = Depends(session, scope="function")):
    return verify_challenge(db, req)


@app.get("/v1/control/tenants/{tenant_id}", dependencies=[Depends(control)])
def tenant(tenant_id: str, db: Session = Depends(session, scope="function")):
    return tenant_status(db, tenant_id)


@app.post("/v1/control/tenants/{tenant_id}/state", dependencies=[Depends(control)])
def state(tenant_id: str, req: StateRequest, db: Session = Depends(session, scope="function")):
    return set_tenant_state(db, tenant_id, req.state, req.expires_at, req.group_limit)


@app.post("/v1/control/runtimes", dependencies=[Depends(control)])
def runtime(req: RuntimeRegistration, db: Session = Depends(session, scope="function")):
    return register_runtime(db, req)


@app.post("/v1/control/groups/claims", dependencies=[Depends(control)])
def claim(req: ClaimRequest, db: Session = Depends(session, scope="function")):
    result = create_group_claim(db, req.tenant_id, req.operation_id)
    result["command"] = "!squire claim " + result["nonce"]
    return result


@app.patch("/v1/control/groups/{conversation_id}", dependencies=[Depends(control)])
def group_update(conversation_id: str, req: GroupSettings, db: Session = Depends(session, scope="function")):
    lock_tenant(db, req.tenant_id)
    binding = db.get(Binding, conversation_id)
    if not binding or binding.tenant_id != req.tenant_id or binding.kind != "group":
        raise HTTPException(404, "group not found")
    if req.active is True and not binding.active:
        raise HTTPException(409, "revoked group requires a renewed administrator claim")
    if req.mode == "all":
        raise HTTPException(403, "a current group administrator must send !squire mode all in the group")
    if req.active is False:
        revoke_group(db, binding)
    if req.mode == "addressed":
        binding.mode = req.mode
        db.add(binding)
    return {"conversation_id": binding.id, "active": binding.active, "mode": binding.mode}


@app.post("/v1/outbound")
def outbound(req: OutboundRequest, runtime: Runtime = Depends(authenticated_runtime), db: Session = Depends(session, scope="function")):
    return enqueue_runtime(db, runtime, req)


@app.get("/v1/outbound/{operation_id}")
def outbound_status(operation_id: str, runtime: Runtime = Depends(authenticated_runtime), db: Session = Depends(session, scope="function")):
    row = db.get(Outbox, operation_id)
    if not row or row.binding_id != runtime.binding_id:
        raise HTTPException(404, "operation not found")
    return {"operation_id": row.id, "status": row.state, "error_code": row.error_code}


@app.post("/v1/operator/provider", dependencies=[Depends(operator)])
def provider(req: ProviderControl, db: Session = Depends(session, scope="function")):
    account = db.exec(select(ProviderAccount).where(ProviderAccount.id == settings().provider_account_id).with_for_update()).one()
    if req.generation is not None:
        if req.generation < account.generation:
            raise HTTPException(409, "generation cannot decrease")
        if req.generation != account.generation and not req.paused:
            raise HTTPException(409, "pause before changing generation")
        account.generation = req.generation
    account.paused = req.paused
    db.add(account)
    return {"paused": account.paused, "generation": account.generation,
            "outbound_enabled": settings().outbound_enabled}


@app.post("/v1/operator/bindings/import", dependencies=[Depends(operator)])
def import_existing(req: ImportBinding, db: Session = Depends(session, scope="function")):
    return import_binding(db, req)


@app.get("/v1/operator/health", dependencies=[Depends(operator)])
def operations(db: Session = Depends(session, scope="function")):
    account = db.get(ProviderAccount, settings().provider_account_id)
    queues = {}
    for name, model in (("inbox", InboxEvent), ("outbox", Outbox)):
        queues[name] = dict(db.exec(select(model.state, func.count()).group_by(model.state)).all())
    checkpoints = db.exec(select(Checkpoint)).all()
    return {"provider_paused": account.paused, "generation": account.generation,
        "outbound_enabled": settings().outbound_enabled, "queues": queues,
        "checkpoint_age_seconds": {r.store_id: max(0, now()-r.updated_at) for r in checkpoints}}


@app.post("/v1/approvals")
def request_approval(req: ShareRequest, runtime: Runtime = Depends(authenticated_runtime), db: Session = Depends(session, scope="function")):
    tenant = lock_tenant(db, runtime.tenant_id)
    group = db.get(Binding, runtime.binding_id)
    if runtime.kind != "group" or not group or not group.active or not eligible(tenant):
        raise HTTPException(403, "active group runtime required")
    key = digest("share-request", runtime.id + "\0" + req.request_key)
    row = db.exec(select(ShareApproval).where(ShareApproval.request_key == key)).first()
    if not row:
        sid = identifier()
        row = ShareApproval(id=sid, tenant_id=runtime.tenant_id, group_binding_id=group.id,
            request_key=key, summary_enc=seal(req.summary, "share-summary:"+sid),
            created_at=now(), expires_at=now()+600)
        db.add(row)
        owner = db.exec(select(Binding).where(Binding.tenant_id == tenant.tenant_id,
            Binding.kind == "dm", Binding.active == True)).first()
        if not owner:
            raise HTTPException(409, "owner unavailable")
        queue_text(db, tenant_id=tenant.tenant_id, binding=owner, response_key="share-request:"+sid,
            text="Your approved group requested a private action: " + req.summary +
                 "\nApprove this request in your Squire account. Any result stays private until you explicitly release it. Request: " + sid)
    return {"approval_id": row.id, "state": row.state, "expires_at": row.expires_at}


@app.get("/v1/approvals")
def runtime_approvals(runtime: Runtime = Depends(authenticated_runtime), db: Session = Depends(session, scope="function")):
    if runtime.kind != "dm":
        raise HTTPException(403, "private runtime required")
    if not eligible(db.get(TenantState, runtime.tenant_id)):
        return {"approvals": []}
    rows = db.exec(select(ShareApproval).join(Binding, Binding.id == ShareApproval.group_binding_id).where(
        ShareApproval.tenant_id == runtime.tenant_id, Binding.active == True,
        ShareApproval.state == "approved", ShareApproval.expires_at > now())).all()
    return {"approvals": [{"approval_id": r.id, "conversation_id": r.group_binding_id,
        "summary": open_sealed(r.summary_enc, "share-summary:"+r.id), "expires_at": r.expires_at} for r in rows]}


@app.post("/v1/approvals/{approval_id}/result")
def private_result(approval_id: str, req: ShareDecision, runtime: Runtime = Depends(authenticated_runtime), db: Session = Depends(session, scope="function")):
    tenant = lock_tenant(db, runtime.tenant_id)
    row = db.exec(select(ShareApproval).where(ShareApproval.id == approval_id).with_for_update()).first()
    group = db.get(Binding, row.group_binding_id) if row else None
    if (runtime.kind != "dm" or not row or row.tenant_id != runtime.tenant_id
        or not eligible(tenant) or not group or not group.active
        or req.tenant_id != runtime.tenant_id or row.state != "approved" or row.expires_at < now() or not req.result):
        raise HTTPException(403, "approval unavailable")
    row.result_enc, row.state = seal(req.result, "share-result:"+row.id), "result_private"
    db.add(row)
    owner = db.get(Binding, runtime.binding_id)
    queue_text(db, tenant_id=row.tenant_id, binding=owner, response_key="share-result:"+row.id,
        text="Private result for group request " + row.id + ":\n" + req.result + "\nThis has not been shared with the group.")
    return {"approval_id": row.id, "state": row.state}


@app.post("/v1/control/approvals/{approval_id}", dependencies=[Depends(control)])
def decide(approval_id: str, req: ShareDecision, db: Session = Depends(session, scope="function")):
    tenant = lock_tenant(db, req.tenant_id)
    row = db.exec(select(ShareApproval).where(ShareApproval.id == approval_id).with_for_update()).first()
    if not row or row.tenant_id != req.tenant_id:
        raise HTTPException(404, "approval not found")
    group = db.get(Binding, row.group_binding_id)
    if row.expires_at < now() or not eligible(tenant) or not group or not group.active:
        raise HTTPException(410, "approval expired or revoked")
    if req.decision == "approve" and row.state == "requested":
        row.state = "approved"
    elif req.decision == "deny" and row.state in {"requested", "approved", "result_private"}:
        row.state, row.result_enc = "denied", None
    elif req.decision == "release" and row.state == "result_private":
        # Human-selected text is bounded by the authenticated control API; group
        # runtime cannot approve or release anything itself.
        result = req.result or open_sealed(row.result_enc, "share-result:"+row.id)
        queue_text(db, tenant_id=row.tenant_id, binding=group,
            response_key="share-release:"+row.id, text=result)
        row.state = "released"
    elif row.state not in {"released", "denied", "approved"}:
        raise HTTPException(409, "invalid approval transition")
    db.add(row)
    return {"approval_id": row.id, "state": row.state}


@app.get("/v1/control/tenants/{tenant_id}/approvals", dependencies=[Depends(control)])
def owner_approvals(tenant_id: str, db: Session = Depends(session, scope="function")):
    rows = db.exec(select(ShareApproval).where(ShareApproval.tenant_id == tenant_id,
        ShareApproval.expires_at > now()).order_by(ShareApproval.created_at.desc()).limit(100)).all()
    return {"approvals": [{"approval_id": r.id, "conversation_id": r.group_binding_id,
        "summary": open_sealed(r.summary_enc, "share-summary:"+r.id), "state": r.state,
        "result": open_sealed(r.result_enc, "share-result:"+r.id) if r.result_enc else None,
        "expires_at": r.expires_at} for r in rows]}
