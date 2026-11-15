"""Atomic journal acceptance and deterministic pre-agent routing."""
import time
from fastapi import HTTPException
from sqlalchemy import func, text as sql_text
from sqlmodel import Session, select
from .config import settings
from .crypto import digest, identifier, seal
from .models import (Binding, Checkpoint, ControlCommand, GroupClaim, InboxEvent, Observation, OptOut,
                     Outbox, PhoneChallenge, Receipt, TenantState)
from .schemas import Event, IngestBatch
from .service import (address_hash, alias_to, binding_for, consume_challenge,
                      eligible, now, set_tenant_state)


def _event_key(event: Event, binding: Binding | None) -> str:
    scope = binding.id if binding else address_hash(event.chat_jid)
    if event.kind == "receipt":
        extra = f"{event.recipient_jid}:{event.receipt_status}:{event.timestamp}"
    elif event.kind == "identity_move":
        extra = event.new_chat_jid or ""
    else:
        extra = ""
    return digest("logical-event", settings().provider_account_id + "\0" + scope + "\0"
                  + event.kind + "\0" + event.provider_message_id + "\0" + extra)


def _receipt(db: Session, event: Event, binding: Binding | None) -> None:
    if not event.receipt_status or not event.recipient_jid:
        return
    rows = db.exec(select(Outbox).where(Outbox.provider_message_id == event.provider_message_id)).all()
    for outbox in rows:
        challenge = db.get(PhoneChallenge, outbox.challenge_id) if outbox.challenge_id else None
        if binding and outbox.binding_id == binding.id:
            direct = binding.kind == "dm"
        elif challenge and challenge.address_hash in {address_hash(x) for x in
                (event.chat_jid, event.chat_phone_jid) if x}:
            direct = True
        else:
            continue
        rid = digest("receipt", outbox.id + "\0" + event.recipient_jid + "\0"
                     + event.receipt_status + "\0" + str(event.timestamp))
        if not db.get(Receipt, rid):
            db.add(Receipt(id=rid, outbox_id=outbox.id, recipient_hash=address_hash(event.recipient_jid),
                           status=event.receipt_status, timestamp=event.timestamp))
        # Group delivery counts remain recipient-specific; not "all delivered".
        if direct:
            target = "read" if event.receipt_status in {"read", "played"} else "delivered"
            rank = {"accepted": 1, "uncertain": 1, "delivered": 2, "read": 3}
            if rank.get(outbox.state, 0) < rank[target] and outbox.state not in {"cancelled", "expired"}:
                outbox.state = target
                outbox.completed_at = now()
                db.add(outbox)


def _opt_out(db: Session, event: Event, binding: Binding | None) -> None:
    for address in {event.chat_jid, event.chat_phone_jid, event.sender_jid, event.sender_phone_jid} - {None, ""}:
        hashed = address_hash(address)
        if not db.get(OptOut, hashed):
            db.add(OptOut(address_hash=hashed, created_at=now()))
    if binding:
        tenant = db.get(TenantState, binding.tenant_id)
        if tenant:
            tenant.opted_out = True
            db.add(tenant)
            set_tenant_state(db, tenant.tenant_id, "paused", tenant.expires_at)
            command_id = digest("tenant-stop", tenant.tenant_id)
            if not db.get(ControlCommand, command_id):
                db.add(ControlCommand(id=command_id, kind="tenant_state:paused",
                    tenant_id=tenant.tenant_id, binding_id="", claim_id=""))
    hashes = [address_hash(x) for x in {event.chat_jid, event.chat_phone_jid} if x]
    for challenge in db.exec(select(PhoneChallenge).where(PhoneChallenge.address_hash.in_(hashes))):
        challenge.expires_at = min(challenge.expires_at, now())
        outbox = db.get(Outbox, challenge.outbox_id)
        if outbox and outbox.state in {"queued", "claimed"}:
            outbox.state, outbox.completed_at = "cancelled", now()
            db.add(outbox)
        db.add(challenge)


def accept_batch(db: Session, batch: IngestBatch) -> dict:
    if batch.provider_account_id != settings().provider_account_id:
        raise HTTPException(403, "provider account mismatch")
    seqs = [e.sequence for e in batch.events]
    if seqs != sorted(seqs) or len(seqs) != len(set(seqs)):
        raise HTTPException(422, "batch sequences must be unique and ordered")
    if db.bind.dialect.name == "postgresql":
        # Serializes checkpoint creation and capacity admission across capture
        # streams. This short transaction never includes provider/network I/O.
        db.execute(sql_text("SELECT pg_advisory_xact_lock(784231903)"))
    checkpoint = db.exec(select(Checkpoint).where(Checkpoint.store_id == batch.store_id).with_for_update()).first()
    if checkpoint is None:
        checkpoint = Checkpoint(store_id=batch.store_id, provider_account_id=batch.provider_account_id,
                                checkpoint=None, updated_at=now())
        db.add(checkpoint)
        db.flush()
    if checkpoint.provider_account_id != batch.provider_account_id:
        raise HTTPException(403, "store account mismatch")
    if checkpoint.checkpoint != batch.checkpoint_before:
        # Response loss after commit: the exact retry is safe only if every entry
        # is already durably observed and the committed checkpoint matches.
        if checkpoint.checkpoint == batch.checkpoint_after and all(
            db.exec(select(Observation).where(Observation.store_id == batch.store_id,
                Observation.sequence == e.sequence, Observation.event_id == e.event_id)).first()
            for e in batch.events):
            return {"accepted": True, "checkpoint": checkpoint.checkpoint,
                    "checkpoint_after": checkpoint.checkpoint, "duplicate": True}
        raise HTTPException(409, "checkpoint conflict; read committed checkpoint")
    if batch.events and batch.checkpoint_before == batch.checkpoint_after:
        raise HTTPException(422, "nonempty batch must advance checkpoint")
    total = db.exec(select(func.count()).select_from(InboxEvent).where(InboxEvent.state.in_(["pending", "claimed"]))).one()
    if total + len(batch.events) > settings().queue_cap_total:
        raise HTTPException(503, "inbox capacity exceeded; retry from committed checkpoint")
    accepted = 0
    for event in batch.events:
        command_tenant = None
        observation = db.exec(select(Observation).where(Observation.store_id == batch.store_id,
                                                        Observation.sequence == event.sequence)).first()
        if observation:
            if observation.event_id != event.event_id:
                raise HTTPException(409, "journal sequence identity changed")
            continue
        binding = binding_for(db, event.chat_jid)
        if binding is None and event.chat_phone_jid:
            binding = binding_for(db, event.chat_phone_jid)
            if binding:
                alias_to(db, event.chat_jid, binding, "transport:" + event.event_id)
        if event.sender_phone_jid and event.sender_jid:
            sender_binding = binding_for(db, event.sender_phone_jid)
            if sender_binding:
                alias_to(db, event.sender_jid, sender_binding, "transport:" + event.event_id)
        if event.kind == "identity_move" and binding and event.new_chat_jid:
            alias_to(db, event.new_chat_jid, binding, "journal:" + event.event_id)
        logical_key = _event_key(event, binding)
        existing = db.exec(select(InboxEvent).where(InboxEvent.logical_key == logical_key)).first()
        if existing:
            # Upserts can reflect edits/current-state repair; never create a new
            # agent turn. Only an undelivered event can change its queued content.
            if existing.state == "pending" and event.kind == "message":
                existing.payload_enc = seal(event.model_dump(), "inbox:" + existing.id)
                db.add(existing)
        else:
            eid = identifier()
            state, payload = "ignored", None
            tenant = db.get(TenantState, binding.tenant_id) if binding else None
            if event.kind == "receipt":
                _receipt(db, event, binding)
            elif event.kind == "delete":
                target_key = _event_key(event.model_copy(update={"kind": "message"}), binding)
                target = db.exec(select(InboxEvent).where(InboxEvent.logical_key == target_key)).first()
                if target:
                    target.payload_enc = None
                    if target.state in {"pending", "claimed"}:
                        target.state, target.completed_at = "cancelled", now()
                    db.add(target)
            elif event.kind == "message" and not event.from_me and event.origin in {"live", "replay"}:
                text = event.text.strip()
                if event.chat_type == "dm" and text.upper() == "STOP":
                    _opt_out(db, event, binding)
                elif event.chat_type == "dm" and text.startswith("SQUIRE-CONNECT "):
                    nonce = text.removeprefix("SQUIRE-CONNECT ").strip()
                    challenge = db.exec(select(PhoneChallenge).where(
                        PhoneChallenge.fallback_hash == digest("fallback", nonce),
                        PhoneChallenge.consumed_at.is_(None), PhoneChallenge.expires_at > now()).with_for_update()).first()
                    if challenge:
                        # A different sender requires website reconfirmation; never
                        # silently rebind to whoever obtained a forwarded nonce.
                        hashes = {address_hash(x) for x in (event.chat_jid, event.chat_phone_jid) if x}
                        if challenge.address_hash in hashes:
                            address = event.chat_phone_jid or event.chat_jid
                            binding = consume_challenge(db, challenge, address)
                            if event.chat_jid != address:
                                alias_to(db, event.chat_jid, binding, "fallback:" + event.event_id)
                elif binding and binding.active and tenant and eligible(tenant):
                    if event.timestamp >= binding.activated_at:
                        pending = db.exec(select(func.count()).select_from(InboxEvent).where(
                            InboxEvent.tenant_id == tenant.tenant_id, InboxEvent.state.in_(["pending", "claimed"]))).one()
                        if pending >= settings().queue_cap_per_tenant:
                            raise HTTPException(503, "tenant inbox capacity exceeded")
                        state, payload = "pending", event.model_dump()
                elif event.chat_type == "group" and re_group_command(text):
                    # Only bounded deterministic group claims are retained before
                    # activation, not arbitrary conversations in unapproved groups.
                    parts = text.split()
                    claim = db.exec(select(GroupClaim).where(GroupClaim.nonce_hash == digest("group-claim", parts[2]),
                        GroupClaim.expires_at > now(), GroupClaim.state.in_(["pending", "awaiting_admin"]))).first() if len(parts) == 3 else None
                    sponsor = db.get(TenantState, claim.tenant_id) if claim else None
                    if sponsor and eligible(sponsor):
                        pending = db.exec(select(func.count()).select_from(InboxEvent).where(
                            InboxEvent.tenant_id == sponsor.tenant_id, InboxEvent.state.in_(["pending", "claimed"]))).one()
                        if pending < settings().queue_cap_per_tenant:
                            state, payload, command_tenant = "pending", event.model_dump(), sponsor.tenant_id
            row = InboxEvent(id=eid, logical_key=logical_key, provider_account_id=batch.provider_account_id,
                provider_message_id=event.provider_message_id, binding_id=binding.id if binding else None,
                tenant_id=binding.tenant_id if binding else command_tenant, kind=event.kind, state=state,
                payload_enc=seal(payload, "inbox:" + eid) if payload else None, created_at=now(),
                completed_at=now() if state == "ignored" else None)
            db.add(row)
            accepted += 1
        db.add(Observation(id=identifier(), store_id=batch.store_id, sequence=event.sequence,
                           event_id=event.event_id, created_at=now()))
        db.flush()
    checkpoint.checkpoint, checkpoint.updated_at = batch.checkpoint_after, now()
    db.add(checkpoint)
    return {"accepted": True, "checkpoint": batch.checkpoint_after,
            "checkpoint_after": batch.checkpoint_after, "events_recorded": accepted}


def re_group_command(text: str) -> bool:
    return (text.startswith("!squire claim ") or text.startswith("!squire approve ")) and len(text) <= 200
