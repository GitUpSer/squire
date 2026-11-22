"""Transactional business operations. Caller owns the session/commit boundary."""
import hmac
import re
import secrets
import time
from urllib.parse import urlsplit
from fastapi import HTTPException
from sqlalchemy import func, or_, update, text as sql_text
from sqlmodel import Session, select
from .config import settings
from .crypto import digest, identifier, open_sealed, seal
from .models import (Binding, Checkpoint, ControlCommand, GroupClaim, IdentityAlias,
                     InboxEvent, Observation, OptOut, Outbox, PhoneChallenge,
                     ProviderAccount, Receipt, Runtime, ShareApproval, TenantState)
from .schemas import (ChallengeRequest, Event, GroupSnapshot, ImportBinding,
                      IngestBatch, OutboundRequest, RuntimeRegistration, VerifyChallenge)


def now() -> float:
    return time.time()


def address_hash(address: str) -> str:
    return digest("address", settings().provider_account_id + "\0" + address)


def phone_jid(phone: str) -> str:
    if not re.fullmatch(r"\+[1-9][0-9]{7,14}", phone):
        raise HTTPException(422, "phone must be an E.164 number")
    return phone[1:] + "@s.whatsapp.net"


def validate_jid(jid: str, kind: str | None = None) -> str:
    if not re.fullmatch(r"[0-9]+(?:-[0-9]+)?@(s\.whatsapp\.net|lid|g\.us)", jid):
        raise HTTPException(422, "invalid recipient identity")
    if kind == "group" and not jid.endswith("@g.us"):
        raise HTTPException(422, "group identity required")
    if kind == "dm" and jid.endswith("@g.us"):
        raise HTTPException(422, "direct identity required")
    return jid


def lock_tenant(db: Session, tenant_id: str) -> TenantState:
    row = db.exec(select(TenantState).where(TenantState.tenant_id == tenant_id).with_for_update()).first()
    if row is None:
        raise HTTPException(404, "tenant not found")
    return row


def eligible(tenant: TenantState, *, setup: bool = False) -> bool:
    states = {"active", "imported_active", "ready_unstarted", "provisioning"}
    if setup:
        states.add("verifying")
    return (tenant.state in states and not tenant.opted_out
            and (tenant.expires_at is None or tenant.expires_at > now()))


def binding_for(db: Session, address: str) -> Binding | None:
    hashed = address_hash(address)
    row = db.exec(select(Binding).where(Binding.address_hash == hashed)).first()
    if row is not None:
        return row
    alias = db.get(IdentityAlias, hashed)
    return db.get(Binding, alias.binding_id) if alias else None


def alias_to(db: Session, alias: str, binding: Binding, evidence: str) -> None:
    validate_jid(alias, binding.kind)
    existing = binding_for(db, alias)
    if existing and existing.id != binding.id:
        raise HTTPException(409, "identity alias conflicts with existing binding")
    key = address_hash(alias)
    if key != binding.address_hash and not db.get(IdentityAlias, key):
        db.add(IdentityAlias(address_hash=key, binding_id=binding.id, evidence=evidence))


def new_binding(db: Session, tenant: TenantState, address: str, kind: str,
                activated_at: float | None = None) -> Binding:
    validate_jid(address, kind)
    existing = binding_for(db, address)
    if existing:
        if existing.tenant_id != tenant.tenant_id:
            raise HTTPException(409, "recipient already bound")
        if not existing.active:
            raise HTTPException(409, "revoked binding requires explicit recovery")
        return existing
    key = identifier()
    row = Binding(id=key, provider_account_id=settings().provider_account_id,
                  tenant_id=tenant.tenant_id, address_hash=address_hash(address),
                  address_enc=seal(address, "binding:" + key), kind=kind,
                  activated_at=activated_at if activated_at is not None else now())
    db.add(row)
    db.flush()
    return row


def queue_capacity(db: Session, tenant_id: str) -> None:
    # A transaction-scoped lock makes the global cap exact across tenants and
    # independent API replicas, without locking the transport dispatch lease.
    if db.bind.dialect.name == "postgresql":
        db.execute(sql_text("SELECT pg_advisory_xact_lock(784231902)"))
    count = db.exec(select(func.count()).select_from(Outbox).where(
        Outbox.tenant_id == tenant_id, Outbox.state.in_(["queued", "claimed", "dispatching"]))).one()
    total = db.exec(select(func.count()).select_from(Outbox).where(
        Outbox.state.in_(["queued", "claimed", "dispatching"]))).one()
    if count >= settings().queue_cap_per_tenant or total >= settings().queue_cap_total:
        raise HTTPException(429, "messaging queue at capacity")


def queue_text(db: Session, *, tenant_id: str, response_key: str, text: str,
               binding: Binding | None = None, challenge_id: str | None = None,
               runtime_id: str | None = None, reply_to: str | None = None,
               priority: int = 0, ttl: float = 86400, media: dict | None = None,
               cause_event_id: str | None = None, expires_at: int | None = None) -> Outbox:
    key = digest("response", tenant_id + "\0" + response_key)
    payload = {"text": text, "reply_to": reply_to}
    if media is not None:
        payload["media"] = media
    existing = db.exec(select(Outbox).where(Outbox.response_key == key)).first()
    if existing:
        if existing.cause_event_id != cause_event_id:
            raise HTTPException(409, "response key reused with different cause")
        if existing.payload_enc and open_sealed(existing.payload_enc, "outbox:" + existing.id) != payload:
            raise HTTPException(409, "response key reused with different content")
        if expires_at is not None:
            existing.expires_at = min(existing.expires_at, expires_at)
            db.add(existing)
        return existing
    queue_capacity(db, tenant_id)
    if media is not None:
        # Includes retained accepted payloads, so delivery cannot bypass the
        # aggregate encrypted attachment storage bound. Cleared by retention.
        own = db.exec(select(func.coalesce(func.sum(func.length(Outbox.payload_enc)), 0)).where(Outbox.tenant_id == tenant_id)).one()
        total = db.exec(select(func.coalesce(func.sum(func.length(Outbox.payload_enc)), 0))).one()
        encrypted_upper_bound = len(media["data_base64"]) * 4 // 3 + 65536
        if own + encrypted_upper_bound > 75 * 1024 * 1024 or total + encrypted_upper_bound > 375 * 1024 * 1024:
            raise HTTPException(429, "attachment storage at capacity")
    oid = identifier()
    row = Outbox(id=oid, response_key=key, tenant_id=tenant_id, runtime_id=runtime_id,
                 binding_id=binding.id if binding else None,
                 binding_generation=binding.generation if binding else None,
                 challenge_id=challenge_id, payload_enc=seal(payload, "outbox:" + oid),
                 priority=priority, created_at=now(), cause_event_id=cause_event_id,
                 expires_at=min(now() + ttl, expires_at) if expires_at is not None else now() + ttl)
    db.add(row)
    db.flush()
    return row


def challenge_response(db: Session, row: PhoneChallenge) -> dict:
    outbox = db.get(Outbox, row.outbox_id)
    jid = str(open_sealed(row.address_enc, "challenge-address:" + row.id))
    nonce = (str(open_sealed(row.fallback_enc, "challenge-fallback:" + row.id))
             if row.expires_at > now() and not row.consumed_at else None)
    return {"challenge_id": row.id, "conversation_id": row.binding_id,
            "delivery_status": outbox.state if outbox else "unknown",
            "expires_at": row.expires_at, "masked_phone": "+••••" + jid.split("@")[0][-4:],
            "fallback_nonce": nonce, "verified": row.consumed_at is not None}


def create_challenge(db: Session, req: ChallengeRequest) -> dict:
    existing = db.exec(select(PhoneChallenge).where(PhoneChallenge.operation_id == req.operation_id)).first()
    if existing:
        if (existing.account_id != req.account_id or existing.tenant_id != req.tenant_id
            or existing.address_hash != address_hash(phone_jid(req.phone))):
            raise HTTPException(409, "operation belongs to another account")
        return challenge_response(db, existing)
    jid = phone_jid(req.phone)
    if db.get(OptOut, address_hash(jid)):
        raise HTTPException(409, "recipient opted out")
    bound = binding_for(db, jid)
    if bound and bound.tenant_id != req.tenant_id:
        raise HTTPException(409, "recipient already bound")
    tenant = db.get(TenantState, req.tenant_id)
    if tenant is None:
        tenant = TenantState(tenant_id=req.tenant_id, account_id=req.account_id,
                             group_limit=settings().group_limit)
        db.add(tenant)
        db.flush()
    tenant = lock_tenant(db, req.tenant_id)
    if tenant.account_id != req.account_id or tenant.state in {"deleted", "expired", "paused"}:
        raise HTTPException(409, "tenant not eligible for verification")
    recent = db.exec(select(func.count()).select_from(PhoneChallenge).where(
        or_(PhoneChallenge.tenant_id == req.tenant_id, PhoneChallenge.address_hash == address_hash(jid)),
        PhoneChallenge.created_at > now() - 3600)).one()
    if recent >= 3:
        raise HTTPException(429, "verification rate limit")
    previous = db.exec(select(PhoneChallenge).where(PhoneChallenge.tenant_id == req.tenant_id,
        PhoneChallenge.consumed_at.is_(None), PhoneChallenge.expires_at > now())).all()
    if previous:
        raise HTTPException(409, "an unexpired verification is already pending")
    cid, code, nonce = identifier(), f"{secrets.randbelow(100000000):08d}", secrets.token_urlsafe(24)
    outbox = queue_text(db, tenant_id=req.tenant_id, response_key="phone:" + cid,
        challenge_id=cid, ttl=settings().challenge_ttl_seconds,
        text=f"Hi, this is Squire. Your setup code is {code}. Enter it on the Squire page to connect your assistant. If you didn't request this, ignore this message or reply STOP.")
    row = PhoneChallenge(id=cid, operation_id=req.operation_id, account_id=req.account_id,
        tenant_id=req.tenant_id, address_hash=address_hash(jid), address_enc=seal(jid, "challenge-address:" + cid),
        code_hash=digest("phone-code:" + cid, code), fallback_hash=digest("fallback", nonce),
        fallback_enc=seal(nonce, "challenge-fallback:" + cid), expires_at=now()+settings().challenge_ttl_seconds,
        created_at=now(), outbox_id=outbox.id, consent_version=req.consent_version)
    db.add(row)
    db.flush()
    return challenge_response(db, row)


def consume_challenge(db: Session, row: PhoneChallenge, address: str) -> Binding:
    tenant = lock_tenant(db, row.tenant_id)
    if tenant.opted_out or tenant.state in {"paused", "expired", "deleted"}:
        raise HTTPException(409, "tenant unavailable")
    binding = new_binding(db, tenant, address, "dm")
    row.consumed_at, row.binding_id = now(), binding.id
    tenant.state = "provisioning"
    pending = db.get(Outbox, row.outbox_id)
    if pending and pending.state in {"queued", "claimed"}:
        pending.state = "cancelled"
    db.add(row)
    db.add(tenant)
    return binding


def verify_challenge(db: Session, req: VerifyChallenge) -> dict:
    row = db.exec(select(PhoneChallenge).where(PhoneChallenge.id == req.challenge_id).with_for_update()).first()
    if row is None or row.account_id != req.account_id:
        raise HTTPException(404, "challenge not found")
    if row.consumed_at is not None:
        return {"verified": True, "tenant_id": row.tenant_id, "conversation_id": row.binding_id}
    if row.expires_at <= now() or row.attempts >= 5:
        raise HTTPException(410, "verification expired")
    row.attempts += 1
    db.add(row)
    if not hmac.compare_digest(row.code_hash, digest("phone-code:" + row.id, req.code)):
        # Failed attempts must survive the HTTP error; there are no other mutations.
        db.commit()
        raise HTTPException(400, "invalid verification code")
    address = str(open_sealed(row.address_enc, "challenge-address:" + row.id))
    binding = consume_challenge(db, row, address)
    return {"verified": True, "tenant_id": row.tenant_id, "conversation_id": binding.id}


def register_runtime(db: Session, req: RuntimeRegistration) -> dict:
    tenant = lock_tenant(db, req.tenant_id)
    binding = db.get(Binding, req.conversation_id)
    if not binding or binding.tenant_id != tenant.tenant_id or binding.kind != req.kind or not binding.active:
        raise HTTPException(403, "runtime scope does not match binding")
    url = urlsplit(req.endpoint)
    if url.scheme not in {"http", "https"} or url.username or url.password or url.query or url.fragment:
        raise HTTPException(422, "invalid runtime endpoint")
    if not url.hostname or not url.hostname.endswith(settings().runtime_host_suffix):
        raise HTTPException(422, "runtime endpoint outside configured private domain")
    if req.ingress_token == req.egress_token:
        raise HTTPException(422, "runtime credential directions must be distinct")
    runtime = db.get(Runtime, req.runtime_id)
    if runtime and (runtime.tenant_id != req.tenant_id or runtime.binding_id != binding.id):
        raise HTTPException(409, "runtime scope is immutable")
    if binding.runtime_id and binding.runtime_id != req.runtime_id:
        raise HTTPException(409, "binding already has a runtime")
    if runtime is None:
        runtime = Runtime(id=req.runtime_id, tenant_id=req.tenant_id, binding_id=binding.id,
            kind=req.kind, endpoint=req.endpoint.rstrip("/"),
            ingress_token_enc=seal(req.ingress_token, "runtime:" + req.runtime_id),
            egress_token_hash=digest("runtime-token", req.egress_token))
    else:
        runtime.endpoint = req.endpoint.rstrip("/")
        runtime.ingress_token_enc = seal(req.ingress_token, "runtime:" + req.runtime_id)
        runtime.egress_token_hash = digest("runtime-token", req.egress_token)
        runtime.active = True
        runtime.generation += 1
    binding.runtime_id = runtime.id
    if tenant.state == "provisioning" and req.kind == "dm":
        tenant.state = "ready_unstarted"
    db.add(runtime)
    db.add(binding)
    db.add(tenant)
    if req.kind == "dm" and tenant.state != "imported_active":
        queue_text(db, tenant_id=tenant.tenant_id, binding=binding,
            runtime_id=runtime.id,
            response_key="introduction:" + binding.id,
            text="Your Squire assistant is ready. What would you like help with? Your 72-hour trial begins when you send your first request.")
    elif req.kind == "group":
        queue_text(db, tenant_id=tenant.tenant_id, binding=binding,
            runtime_id=runtime.id,
            response_key="group-introduction:" + binding.id,
            text="Squire is connected to this approved group. I use this group's own context, not anyone's private conversations or accounts. Address me with !squire or reply to one of my messages. A group administrator can pause me with !squire stop.")
    return {"registered": True, "runtime_id": runtime.id, "conversation_id": binding.id}


def tenant_status(db: Session, tenant_id: str) -> dict:
    tenant = db.get(TenantState, tenant_id)
    if tenant is None:
        return {"tenant_id": tenant_id, "state": "unregistered", "groups": [], "verified": False}
    bindings = db.exec(select(Binding).where(Binding.tenant_id == tenant_id)).all()
    dm = next((b for b in bindings if b.kind == "dm"), None)
    challenge = db.exec(select(PhoneChallenge).where(PhoneChallenge.tenant_id == tenant_id)
                        .order_by(PhoneChallenge.created_at.desc())).first()
    result = {"tenant_id": tenant_id, "state": tenant.state,
        "conversation_id": dm.id if dm else None, "verified": bool(dm and dm.active),
        "runtime_ready": bool(dm and dm.runtime_id), "opted_out": tenant.opted_out,
        "groups": [{"conversation_id": b.id, "active": b.active, "mode": b.mode,
                    "runtime_id": b.runtime_id, "ready": bool(b.runtime_id)} for b in bindings if b.kind == "group"]}
    if challenge:
        result["phone_challenge"] = challenge_response(db, challenge)
    return result


def create_group_claim(db: Session, tenant_id: str, operation_id: str) -> dict:
    tenant = lock_tenant(db, tenant_id)
    if not eligible(tenant):
        raise HTTPException(403, "tenant not active")
    dm = db.exec(select(Binding).where(Binding.tenant_id == tenant_id, Binding.kind == "dm", Binding.active == True)).first()
    if not dm:
        raise HTTPException(409, "verify owner WhatsApp first")
    existing = db.exec(select(GroupClaim).where(GroupClaim.operation_id == operation_id)).first()
    if existing and existing.tenant_id != tenant_id:
        raise HTTPException(409, "operation belongs to another tenant")
    if existing:
        return {"claim_id": existing.id, "nonce": open_sealed(existing.nonce_enc, "claim:"+existing.id), "expires_at": existing.expires_at, "state": existing.state}
    groups = db.exec(select(func.count()).select_from(Binding).where(Binding.tenant_id == tenant_id, Binding.kind == "group", Binding.active == True)).one()
    if groups >= tenant.group_limit:
        raise HTTPException(409, "group allowance reached")
    cid, nonce = identifier(), secrets.token_urlsafe(18)
    row = GroupClaim(id=cid, operation_id=operation_id, tenant_id=tenant_id,
        nonce_hash=digest("group-claim", nonce), nonce_enc=seal(nonce,"claim:"+cid),
        expires_at=now()+settings().group_claim_ttl_seconds)
    db.add(row)
    return {"claim_id": cid, "nonce": nonce, "expires_at": row.expires_at, "state": row.state}


def set_tenant_state(db: Session, tenant_id: str, state: str, expires_at: float | None,
                     group_limit: int | None = None) -> dict:
    tenant = lock_tenant(db, tenant_id)
    tenant.state, tenant.expires_at = state, expires_at
    if group_limit is not None:
        tenant.group_limit = group_limit
    db.add(tenant)
    if state in {"paused", "expired", "deleted"}:
        for row in db.exec(select(Outbox).where(Outbox.tenant_id == tenant_id, Outbox.state.in_(["queued", "claimed"]))):
            row.state, row.completed_at = "cancelled", now()
            db.add(row)
        for row in db.exec(select(InboxEvent).where(InboxEvent.tenant_id == tenant_id, InboxEvent.state.in_(["pending", "claimed"]))):
            row.state, row.completed_at = "cancelled", now()
            db.add(row)
    if state == "deleted":
        for binding in db.exec(select(Binding).where(Binding.tenant_id == tenant_id)):
            binding.active, binding.generation = False, binding.generation + 1
            binding.address_enc = seal("deleted", "binding:" + binding.id)
            db.add(binding)
        for row in db.exec(select(Runtime).where(Runtime.tenant_id == tenant_id)):
            row.active, row.egress_token_hash = False, digest("revoked", identifier())
            row.ingress_token_enc = seal("revoked", "runtime:" + row.id)
            db.add(row)
        for model in (InboxEvent, Outbox):
            for row in db.exec(select(model).where(model.tenant_id == tenant_id)):
                row.payload_enc = None
                db.add(row)
        for row in db.exec(select(PhoneChallenge).where(PhoneChallenge.tenant_id == tenant_id)):
            row.address_enc = seal("deleted", "challenge-address:"+row.id)
            row.fallback_enc = seal("deleted", "challenge-fallback:"+row.id)
            row.expires_at = min(row.expires_at, now())
            db.add(row)
        for row in db.exec(select(GroupClaim).where(GroupClaim.tenant_id == tenant_id)):
            row.nonce_enc, row.group_enc = seal("deleted", "claim:"+row.id), None
            row.state, row.expires_at = "revoked", min(row.expires_at, now())
            db.add(row)
        for row in db.exec(select(ShareApproval).where(ShareApproval.tenant_id == tenant_id)):
            row.summary_enc, row.result_enc = seal("deleted", "share-summary:"+row.id), None
            row.state, row.expires_at = "revoked", min(row.expires_at, now())
            db.add(row)
    return {"tenant_id": tenant_id, "state": state}


def import_binding(db: Session, req: ImportBinding) -> dict:
    if db.bind.dialect.name == "postgresql":
        # Share capture's lock so a pre-binding observation cannot race import.
        db.execute(sql_text("SELECT pg_advisory_xact_lock(784231903)"))
    tenant = db.get(TenantState, req.tenant_id)
    if tenant is None:
        tenant = TenantState(tenant_id=req.tenant_id, account_id=req.account_id, state="imported_active", group_limit=req.group_limit)
        db.add(tenant)
        db.flush()
    if tenant.account_id != req.account_id:
        raise HTTPException(409, "account mismatch")
    tenant = lock_tenant(db, req.tenant_id)
    binding = new_binding(db, tenant, req.recipient, req.kind, req.activated_at)
    if (binding.kind != req.kind or binding.provider_account_id != settings().provider_account_id
        or binding.activated_at != req.activated_at):
        raise HTTPException(409, "import binding scope or replay boundary changed")
    for alias in req.aliases:
        alias_to(db, alias, binding, "operator-migration:" + req.operation_id)
    if req.kind == "group":
        if not req.owner_recipient:
            raise HTTPException(422, "group owner identity required")
        owner = binding_for(db, req.owner_recipient)
        if not owner or owner.tenant_id != tenant.tenant_id or owner.kind != "dm":
            raise HTTPException(403, "group owner must be bound founder")
        binding.owner_identity_hash = address_hash(req.owner_recipient)
        db.add(binding)
    # Payload-free source receipts use the same durable identity as transport
    # replay. They may only be installed before any different work exists.
    from .ingest import _event_key
    marker = "migration_handled:" + digest("migration-import", req.operation_id)
    for message_id in sorted(set(req.handled_provider_message_ids)):
        event = Event(event_id="migration", sequence=0, provider_message_id=message_id,
            chat_jid=req.recipient, chat_type=req.kind, timestamp=req.activated_at, expires_at=None)
        key = _event_key(event, binding)
        unbound_keys = [_event_key(event.model_copy(update={"chat_jid":address}), None)
                        for address in {req.recipient, *req.aliases}]
        if db.exec(select(InboxEvent.id).where(InboxEvent.logical_key.in_(unbound_keys))).first():
            raise HTTPException(409, "source receipt conflicts with pre-binding observation; reconciliation required")
        existing = db.exec(select(InboxEvent).where(InboxEvent.logical_key == key)).first()
        if existing:
            if (existing.state != "done" or existing.error_code != marker or existing.payload_enc is not None
                or existing.binding_id != binding.id or existing.tenant_id != tenant.tenant_id):
                raise HTTPException(409, "source receipt conflicts with existing event; reconciliation required")
            continue
        db.add(InboxEvent(id=identifier(), logical_key=key, provider_account_id=settings().provider_account_id,
            provider_message_id=message_id, binding_id=binding.id, tenant_id=tenant.tenant_id,
            kind="message", state="done", payload_enc=None, error_code=marker,
            created_at=now(), completed_at=now()))
    return {"tenant_id": tenant.tenant_id, "conversation_id": binding.id}


def enqueue_runtime(db: Session, runtime: Runtime, req: OutboundRequest) -> dict:
    tenant = lock_tenant(db, runtime.tenant_id)
    binding = db.get(Binding, runtime.binding_id)
    if (req.conversation_id != runtime.binding_id or not runtime.active or not binding
        or not binding.active or not eligible(tenant)):
        raise HTTPException(403, "outbound scope unavailable")
    event = None
    if req.cause_event_id:
        event = db.get(InboxEvent, req.cause_event_id)
        if not event or event.binding_id != binding.id or event.tenant_id != runtime.tenant_id:
            raise HTTPException(403, "response event scope mismatch")
        if event.state in {"expired", "cancelled"} or (event.expires_at is not None and event.expires_at <= now()):
            raise HTTPException(410, "response event expired or revoked")
    outbox = queue_text(db, tenant_id=runtime.tenant_id, runtime_id=runtime.id,
        response_key=runtime.id + ":" + req.response_key, text=req.text, binding=binding,
        reply_to=req.reply_to, priority=1 if req.priority == "scheduled" else 0,
        media=req.media.model_dump() if req.media else None, cause_event_id=req.cause_event_id,
        expires_at=event.expires_at if event else None)
    return {"operation_id": outbox.id, "status": outbox.state}
