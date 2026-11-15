"""Group claims require an authenticated owner and a current administrator."""
from fastapi import HTTPException
from sqlmodel import Session, select
from sqlalchemy import func
from .config import settings
from .crypto import digest, identifier, open_sealed, seal
from .models import Binding, ControlCommand, GroupClaim, Outbox, Runtime, ShareApproval, TenantState
from .schemas import Event, GroupSnapshot
from .service import address_hash, binding_for, eligible, lock_tenant, new_binding, now, queue_text


def identity_matches(db: Session, left: str, right: str) -> bool:
    if left == right:
        return True
    a, b = binding_for(db, left), binding_for(db, right)
    return bool(a and b and a.id == b.id)


def fresh_snapshot(event: Event, snapshot: GroupSnapshot) -> None:
    if snapshot.jid and snapshot.jid != event.chat_jid:
        raise HTTPException(403, "group snapshot scope mismatch")
    age = now() - snapshot.fetched_at
    if age < -5 or age > settings().group_metadata_ttl_seconds:
        raise HTTPException(503, "fresh group metadata required")


def sender_admin(db: Session, event: Event, snapshot: GroupSnapshot) -> bool:
    fresh_snapshot(event, snapshot)
    return any(p.admin and identity_matches(db, candidate, sender)
               for p in snapshot.participants for candidate in (p.jid, p.phone_jid) if candidate
               for sender in (event.sender_jid, event.sender_phone_jid) if sender)


def owner_present(db: Session, tenant_id: str, snapshot: GroupSnapshot) -> bool:
    for participant in snapshot.participants:
        binding = binding_for(db, participant.phone_jid or participant.jid)
        if binding and binding.tenant_id == tenant_id and binding.kind == "dm" and binding.active:
            return True
    return False


def revoke_group(db: Session, binding: Binding) -> None:
    if not binding.active:
        return
    # A later reapproval must never resurrect a private action authorized under
    # the old group binding. Erase unreleased private results on revocation.
    for approval in db.exec(select(ShareApproval).where(ShareApproval.group_binding_id == binding.id,
            ShareApproval.state.in_(["requested", "approved", "result_private"]))):
        approval.state, approval.result_enc = "denied", None
        db.add(approval)
    binding.active = False
    binding.generation += 1
    db.add(binding)
    runtime = db.get(Runtime, binding.runtime_id) if binding.runtime_id else None
    if runtime:
        runtime.active = False
        db.add(runtime)
    command_id = digest("revoke-group", binding.id + ":" + str(binding.generation))
    if not db.get(ControlCommand, command_id):
        db.add(ControlCommand(id=command_id, kind="group_state:paused", tenant_id=binding.tenant_id,
            binding_id=binding.id, binding_generation=binding.generation, claim_id=""))
    for row in db.exec(select(Outbox).where(Outbox.binding_id == binding.id,
                                           Outbox.state.in_(["queued", "claimed"]))):
        row.state, row.completed_at = "cancelled", now()
        db.add(row)


def handle_command(db: Session, event: Event, snapshot: GroupSnapshot) -> bool:
    """Return true if the event was consumed as a deterministic control command."""
    text = event.text.strip()
    if not text.startswith("!squire "):
        return False
    fresh_snapshot(event, snapshot)
    parts = text.split()
    binding = binding_for(db, event.chat_jid)
    if text == "!squire stop":
        if binding and binding.kind == "group" and sender_admin(db, event, snapshot):
            revoke_group(db, binding)
        return True
    if len(parts) == 3 and parts[1] == "mode" and parts[2] in {"all", "addressed"}:
        if binding and binding.active and sender_admin(db, event, snapshot):
            binding.mode, binding.membership_checked_at = parts[2], now()
            db.add(binding)
        return True
    if len(parts) != 3 or parts[1] not in {"claim", "approve"}:
        return False
    claim = db.exec(select(GroupClaim).where(GroupClaim.nonce_hash == digest("group-claim", parts[2])).with_for_update()).first()
    if not claim or claim.expires_at <= now() or claim.state == "activated":
        return True
    tenant = lock_tenant(db, claim.tenant_id)
    if not eligible(tenant) or not owner_present(db, tenant.tenant_id, snapshot):
        return True
    if binding and binding.tenant_id != tenant.tenant_id:
        claim.state = "conflict"
        db.add(claim)
        return True
    if parts[1] == "claim":
        owner = binding_for(db, event.sender_phone_jid or event.sender_jid)
        if not owner or owner.tenant_id != tenant.tenant_id or owner.kind != "dm" or not owner.active:
            return True
        if claim.group_hash and claim.group_hash != address_hash(event.chat_jid):
            return True
        claim.group_hash = address_hash(event.chat_jid)
        claim.group_enc = seal(event.chat_jid, "claim-group:" + claim.id)
        claim.owner_hash = owner.address_hash
        claim.state = "awaiting_admin"
        if not sender_admin(db, event, snapshot):
            queue_text(db, tenant_id=tenant.tenant_id, binding=owner,
                response_key="group-admin-needed:" + claim.id,
                text="Ask a current group administrator to send !squire approve " + parts[2] + " in that same group to finish connecting Squire.",
                ttl=max(1, claim.expires_at-now()))
            db.add(claim)
            return True
    elif (claim.state != "awaiting_admin" or claim.group_hash != address_hash(event.chat_jid)
          or not sender_admin(db, event, snapshot)):
        return True
    groups = db.exec(select(func.count()).select_from(Binding).where(
        Binding.tenant_id == tenant.tenant_id, Binding.kind == "group", Binding.active == True)).one()
    if (not binding or not binding.active) and groups >= tenant.group_limit:
        claim.state = "limit_reached"
        db.add(claim)
        return True
    if binding and not binding.active:
        # Same sponsor, newly proven owner membership and administrator consent.
        binding.active = True
        binding.generation += 1
        binding.runtime_id = None
        binding.mode = "addressed"
        binding.activated_at = now()
        db.add(binding)
    group = new_binding(db, tenant, event.chat_jid, "group")
    group.owner_identity_hash = claim.owner_hash
    group.admin_identity_hash = address_hash(event.sender_phone_jid or event.sender_jid)
    group.membership_checked_at = now()
    group.owner_present = True
    claim.state, claim.binding_id = "activated", group.id
    claim.administrator_hash = group.admin_identity_hash
    db.add(group)
    db.add(claim)
    command_id = digest("activate-group", claim.id)
    if not db.get(ControlCommand, command_id):
        db.add(ControlCommand(id=command_id, kind="activate_group", tenant_id=tenant.tenant_id,
                              binding_id=group.id, binding_generation=group.generation, claim_id=claim.id))
    return True


def participates(db: Session, binding: Binding, event: Event, snapshot: GroupSnapshot) -> bool:
    fresh_snapshot(event, snapshot)
    binding.membership_checked_at = now()
    binding.owner_present = owner_present(db, binding.tenant_id, snapshot)
    db.add(binding)
    if not binding.owner_present:
        revoke_group(db, binding)
        return False
    if binding.mode == "all" or event.text.lstrip().startswith("!squire"):
        return True
    if event.reply_to:
        ours = db.exec(select(Outbox).where(Outbox.binding_id == binding.id,
            Outbox.provider_message_id == event.reply_to,
            Outbox.state.in_(["accepted", "delivered", "read"]))).first()
        if ours:
            return True
    # v0.26.0 does not expose native incoming mentions. Never infer identity
    # from display-name text. A future provider can add a verified capability.
    return False
