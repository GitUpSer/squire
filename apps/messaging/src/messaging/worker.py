"""Durable workers; outbound ambiguity is a state, never an automatic resend."""
import concurrent.futures
import logging
import signal
import threading
import time
from dataclasses import dataclass
import httpx
from fastapi import HTTPException
from sqlalchemy import and_, or_, update
from sqlmodel import Session, select
from .config import settings
from .crypto import digest, identifier, open_sealed, seal
from .db import engine, verify_schema
from .groups import handle_command, participates, fresh_snapshot, owner_present, revoke_group
from .models import (Binding, ControlCommand, GroupClaim, InboxEvent, Outbox, PhoneChallenge,
                     ProviderAccount, Runtime, ShareApproval, TenantState)
from .schemas import Event, GroupSnapshot
from .service import eligible, now

log = logging.getLogger(__name__)


@dataclass
class Claim:
    id: str
    owner: str
    fence: int


def backoff(attempt: int) -> float:
    return min(300, 2 ** min(attempt, 8))


def recover_leases() -> None:
    with Session(engine()) as db:
        for event in db.exec(select(InboxEvent).where(InboxEvent.state == "claimed", InboxEvent.lease_until < now()).with_for_update(skip_locked=True)):
            event.state, event.error_code = "pending", "worker_lease_expired"
            db.add(event)
        for out in db.exec(select(Outbox).where(Outbox.state.in_(["claimed", "dispatching"]), Outbox.lease_until < now()).with_for_update(skip_locked=True)):
            out.state = "uncertain" if out.state == "dispatching" else "queued"
            out.error_code = "worker_lease_expired"
            db.add(out)
        provider = db.get(ProviderAccount, settings().provider_account_id)
        if provider and provider.send_lease_until < now():
            provider.send_owner = None
            db.add(provider)
        db.commit()


def claim_inbound() -> Claim | None:
    with Session(engine()) as db:
        candidates = db.exec(select(InboxEvent).outerjoin(TenantState, InboxEvent.tenant_id == TenantState.tenant_id)
            .where(InboxEvent.state == "pending", InboxEvent.next_attempt_at <= now())
            .order_by((InboxEvent.kind == "group_command").desc(), TenantState.last_dispatched_at.asc().nullsfirst(), InboxEvent.created_at, InboxEvent.id)
            .limit(32).with_for_update(skip_locked=True, of=InboxEvent)).all()
        for row in candidates:
            binding = None
            if row.binding_id:
                binding = db.exec(select(Binding).where(Binding.id == row.binding_id).with_for_update(skip_locked=True)).first()
                if not binding or (binding.processing_event_id and binding.processing_lease_until > now()):
                    continue
                earliest = db.exec(select(InboxEvent.id).where(InboxEvent.binding_id == binding.id,
                    InboxEvent.state.in_(["pending", "claimed"]))
                    .order_by(InboxEvent.created_at, InboxEvent.id).limit(1)).first()
                if earliest != row.id and row.kind != "group_command":
                    continue
            owner = identifier()
            row.state, row.lease_owner, row.lease_until = "claimed", owner, now() + settings().lease_seconds
            row.attempts += 1
            row.fence += 1
            if binding:
                binding.processing_event_id, binding.processing_lease_until = row.id, row.lease_until
                db.add(binding)
            if row.tenant_id:
                tenant = db.get(TenantState, row.tenant_id)
                if tenant:
                    tenant.last_dispatched_at = now()
                    db.add(tenant)
            db.add(row)
            claim = Claim(row.id, owner, row.fence)
            db.commit()
            return claim
    return None


def _claim_row(db: Session, model, claim: Claim):
    row = db.exec(select(model).where(model.id == claim.id).with_for_update()).first()
    if (not row or row.lease_owner != claim.owner or row.fence != claim.fence
        or row.lease_until < now()):
        return None
    return row


def finish_inbound(claim: Claim, state: str, error: str | None = None) -> None:
    with Session(engine()) as db:
        row = _claim_row(db, InboxEvent, claim)
        if not row or row.state != "claimed":
            return
        row.state, row.error_code = state, error
        row.next_attempt_at = now() + backoff(row.attempts) if state == "pending" else 0
        row.completed_at = now() if state != "pending" else None
        if state == "expired":
            row.payload_enc = None
        if row.binding_id:
            binding = db.get(Binding, row.binding_id)
            if binding and binding.processing_event_id == row.id:
                binding.processing_event_id, binding.processing_lease_until = None, 0
                db.add(binding)
        db.add(row)
        db.commit()


def _transport_headers() -> dict:
    return {"Authorization": "Bearer " + settings().transport_broker_token}


def _control_headers() -> dict:
    return {"Authorization": "Bearer " + settings().control_callback_token}


def group_snapshot(client: httpx.Client, event: Event) -> GroupSnapshot:
    # Privileged operations use a fresh provider lookup, never caller text or
    # an address-book name. The transport serializes its own sync pause.
    response = client.get(settings().transport_url.rstrip("/") + "/v1/groups/" + event.chat_jid,
                          headers=_transport_headers(), timeout=45)
    response.raise_for_status()
    return GroupSnapshot.model_validate(response.json())


def dispatch(claim: Claim, client: httpx.Client) -> None:
    try:
        with Session(engine()) as db:
            row = _claim_row(db, InboxEvent, claim)
            if not row or row.state != "claimed" or not row.payload_enc:
                return
            if row.expires_at is not None and row.expires_at <= now():
                db.commit()
                finish_inbound(claim, "expired", "message_expired")
                return
            event = Event.model_validate(open_sealed(row.payload_enc, "inbox:" + row.id))
        snapshot = group_snapshot(client, event) if event.chat_type == "group" else None
        with Session(engine()) as db:
            row = _claim_row(db, InboxEvent, claim)
            if not row or row.state != "claimed":
                return
            if row.expires_at is not None and row.expires_at <= now():
                db.commit()
                finish_inbound(claim, "expired", "message_expired")
                return
            if snapshot and handle_command(db, event, snapshot):
                db.commit()
                finish_inbound(claim, "done")
                return
            binding = db.get(Binding, row.binding_id) if row.binding_id else None
            tenant = db.get(TenantState, binding.tenant_id) if binding else None
            if not binding or not binding.active or not tenant or not eligible(tenant):
                db.commit()
                finish_inbound(claim, "cancelled", "binding_unavailable")
                return
            if snapshot:
                fresh_snapshot(event, snapshot)
                if not owner_present(db, binding.tenant_id, snapshot):
                    revoke_group(db, binding)
                    db.commit()
                    finish_inbound(claim, "cancelled", "owner_left_group")
                    return
            runtime = db.get(Runtime, binding.runtime_id) if binding.runtime_id else None
            if not runtime or not runtime.active:
                db.commit()
                finish_inbound(claim, "pending", "runtime_unavailable")
                return
            if snapshot and not participates(db, binding, event, snapshot):
                db.commit()
                finish_inbound(claim, "ignored")
                return
            runtime_id, tenant_id = runtime.id, tenant.tenant_id
            endpoint, ingress_token = runtime.endpoint, open_sealed(runtime.ingress_token_enc, "runtime:" + runtime.id)
            envelope = {"event_id": row.id, "conversation_id": binding.id,
                "tenant_id": tenant_id, "runtime_id": runtime_id, "chat_type": binding.kind,
                "sender_id": digest("participant", binding.id + "\0" + event.sender_jid),
                "provider_message_id": event.provider_message_id, "timestamp": event.timestamp,
                "payload": {"text": event.text, "reply_to": event.reply_to,
                            "mentions": event.mentions, "media": event.media,
                            "expires_at": row.expires_at}}
            db.commit()
        # Admission and runtime delivery are both idempotent by the stable event ID.
        admission = client.post(settings().control_url.rstrip("/") + f"/v1/service/tenants/{tenant_id}/admit",
            headers=_control_headers(), json={"operation_id": claim.id, "runtime_id": runtime_id}, timeout=20)
        admission.raise_for_status()
        decision = admission.json()
        if decision.get("allowed") is not True:
            reason = decision.get("reason", "entitlement_unavailable")
            if reason in {"activating", "runtime_unavailable", "provisioning", "runtime_not_ready"}:
                finish_inbound(claim, "pending", reason)
            else:
                finish_inbound(claim, "cancelled", "entitlement_denied")
            return
        with Session(engine()) as db:
            row = _claim_row(db, InboxEvent, claim)
            binding = db.get(Binding, row.binding_id) if row else None
            tenant = db.get(TenantState, tenant_id)
            if not row or row.state != "claimed" or not binding or not binding.active or not tenant or not eligible(tenant):
                return
            if row.expires_at is not None and row.expires_at <= now():
                db.commit()
                finish_inbound(claim, "expired", "message_expired")
                return
            envelope["payload"]["expires_at"] = row.expires_at
        response = client.post(endpoint, headers={"Authorization": "Bearer " + str(ingress_token)},
                               json={"events": [envelope]}, timeout=20)
        response.raise_for_status()
        if response.json().get("accepted") is not True:
            finish_inbound(claim, "pending", "runtime_no_durable_ack")
            return
        finish_inbound(claim, "done")
    except (httpx.HTTPError, ValueError, HTTPException):
        finish_inbound(claim, "pending", "dispatch_dependency_unavailable")
    except Exception:
        # No exception repr: provider errors can contain message bodies/addresses.
        log.error("dispatch internal failure for event %s", claim.id)
        finish_inbound(claim, "pending", "dispatch_internal_error")


def claim_outbound() -> Claim | None:
    if not settings().outbound_enabled:
        return None
    with Session(engine()) as db:
        account = db.exec(select(ProviderAccount).where(ProviderAccount.id == settings().provider_account_id)
                          .with_for_update(skip_locked=True)).first()
        if (not account or account.paused or account.send_lease_until > now()
            or account.last_sent_at + settings().send_spacing_seconds > now()):
            return None
        row = db.exec(select(Outbox).join(TenantState, Outbox.tenant_id == TenantState.tenant_id)
            .where(Outbox.state == "queued", Outbox.next_attempt_at <= now())
            .order_by(Outbox.priority, TenantState.last_sent_at, Outbox.created_at, Outbox.id)
            .limit(1).with_for_update(skip_locked=True, of=Outbox)).first()
        if not row:
            return None
        owner = identifier()
        row.state, row.lease_owner, row.lease_until = "claimed", owner, now() + settings().lease_seconds
        row.fence += 1
        row.attempts += 1
        account.send_owner, account.send_lease_until = owner, row.lease_until
        db.add(account)
        db.add(row)
        claim = Claim(row.id, owner, row.fence)
        db.commit()
        return claim


def finish_outbound(claim: Claim, state: str, provider_id: str | None = None,
                    error: str | None = None) -> None:
    with Session(engine()) as db:
        row = _claim_row(db, Outbox, claim)
        account = db.get(ProviderAccount, settings().provider_account_id)
        if not row or row.state not in {"claimed", "dispatching", "uncertain"}:
            return
        if state == "queued" and row.expires_at <= now():
            state, error = "cancelled", "message_expired"
        row.state, row.error_code = state, error
        row.provider_message_id = provider_id or row.provider_message_id
        row.completed_at = now() if state not in {"queued", "claimed"} else None
        if state == "queued":
            row.next_attempt_at = now() + backoff(row.attempts)
        if account and account.send_owner == claim.owner:
            account.send_owner, account.send_lease_until = None, 0
            account.last_sent_at = now()
            db.add(account)
        tenant = db.get(TenantState, row.tenant_id)
        if tenant:
            tenant.last_sent_at = now()
            db.add(tenant)
        db.add(row)
        db.commit()


def send(claim: Claim, client: httpx.Client) -> None:
    submitted = False
    try:
        # Recheck authoritative entitlement/readiness immediately before every
        # non-verification send, including intros queued during provisioning.
        with Session(engine()) as db:
            candidate = _claim_row(db, Outbox, claim)
            if not candidate or candidate.state != "claimed":
                return
            tenant_id, challenge_id = candidate.tenant_id, candidate.challenge_id
            runtime_id = candidate.runtime_id
            binding = db.get(Binding, candidate.binding_id) if candidate.binding_id else None
            group_jid = (open_sealed(binding.address_enc, "binding:"+binding.id)
                         if binding and binding.kind == "group" else None)
        if not challenge_id:
            response = client.get(settings().control_url.rstrip("/") + f"/v1/service/tenants/{tenant_id}/entitlement",
                headers=_control_headers(), params={"runtime_id": runtime_id} if runtime_id else None, timeout=20)
            response.raise_for_status()
            entitlement = response.json()
            if entitlement.get("state") in {"paused", "expired", "deleted"}:
                finish_outbound(claim, "cancelled", error="entitlement_denied")
                return
            if (entitlement.get("runtime_ready") is not True or
                (entitlement.get("allowed") is not True and entitlement.get("state") != "ready_unstarted")):
                finish_outbound(claim, "queued", error="runtime_or_entitlement_unavailable")
                return
        snapshot = None
        if group_jid:
            synthetic = Event(event_id="send-check", sequence=0, provider_message_id="send-check",
                              chat_jid=group_jid, chat_type="group", timestamp=now(), expires_at=None)
            snapshot = group_snapshot(client, synthetic)
            fresh_snapshot(synthetic, snapshot)
        with Session(engine()) as db:
            row = _claim_row(db, Outbox, claim)
            account = db.get(ProviderAccount, settings().provider_account_id)
            if not row or row.state != "claimed":
                return
            if (not account or account.paused or account.send_owner != claim.owner
                or account.send_lease_until < now()):
                return
            tenant = db.get(TenantState, row.tenant_id)
            if row.cause_event_id:
                parent = db.get(InboxEvent, row.cause_event_id)
                if (not parent or parent.binding_id != row.binding_id
                        or parent.state in {"expired", "cancelled"}):
                    db.commit()
                    finish_outbound(claim, "cancelled", error="parent_unavailable")
                    return
                if parent.expires_at is not None:
                    row.expires_at = min(row.expires_at, parent.expires_at)
            if not tenant or not eligible(tenant, setup=bool(row.challenge_id)) or row.expires_at <= now() or not row.payload_enc:
                db.commit()
                finish_outbound(claim, "cancelled", error="entitlement_or_expiry")
                return
            if row.challenge_id:
                challenge = db.get(PhoneChallenge, row.challenge_id)
                if not challenge or challenge.consumed_at or challenge.expires_at < now():
                    db.commit()
                    finish_outbound(claim, "cancelled", error="challenge_unavailable")
                    return
                recipient = open_sealed(challenge.address_enc, "challenge-address:" + challenge.id)
            else:
                binding = db.get(Binding, row.binding_id)
                if (not binding or not binding.active or binding.generation != row.binding_generation
                    or (row.runtime_id and binding.runtime_id != row.runtime_id)):
                    db.commit()
                    finish_outbound(claim, "cancelled", error="binding_revoked")
                    return
                if snapshot and not owner_present(db, binding.tenant_id, snapshot):
                    revoke_group(db, binding)
                    db.commit()
                    finish_outbound(claim, "cancelled", error="group_owner_removed")
                    return
                recipient = open_sealed(binding.address_enc, "binding:" + binding.id)
            payload = open_sealed(row.payload_enc, "outbox:" + row.id)
            outgoing = {"operation_id": row.id, "provider_account_id": account.id,
                "generation": account.generation, "recipient": recipient, "kind": "text",
                "text": payload["text"], "reply_to": payload.get("reply_to"),
                "expires_at": int(row.expires_at)}
            if payload.get("media"):
                outgoing.update(kind="media", media=payload["media"])
            # Commit intent before network. A crash after this point becomes
            # uncertain; the broker's same operation ID may reconcile it safely.
            # Renew both fences only after every dependency and authorization
            # check, reserving a full lease for the bounded provider operation.
            row.lease_until = now() + settings().lease_seconds
            account.send_lease_until = row.lease_until
            db.add(account)
            row.state = "dispatching"
            db.add(row)
            db.commit()
        submitted = True
        response = client.post(settings().transport_url.rstrip("/") + "/v1/send",
                               headers=_transport_headers(), json=outgoing, timeout=60)
        response.raise_for_status()
        result = response.json()
        if result.get("status") == "accepted" and result.get("provider_message_id"):
            finish_outbound(claim, "accepted", result["provider_message_id"])
        elif result.get("status") == "definitely_unsent":
            # Only the provider's explicit not-dispatched result permits retry.
            code = result.get("error_code", "provider_not_sent")
            if code in {"account_restricted", "logged_out", "generation_mismatch"}:
                with Session(engine()) as db:
                    account = db.get(ProviderAccount, settings().provider_account_id)
                    account.paused = True
                    db.add(account)
                    db.commit()
                finish_outbound(claim, "failed", error=code)
            else:
                retryable = result.get("retryable") is True
                finish_outbound(claim, "queued" if retryable else "failed",
                                error="provider_definitely_unsent")
        else:
            finish_outbound(claim, "uncertain", error="provider_outcome_uncertain")
    except Exception:
        finish_outbound(claim, "uncertain" if submitted else "queued",
                        error="transport_outcome_uncertain" if submitted else "send_preparation_failed")


def deliver_control_commands(client: httpx.Client) -> None:
    with Session(engine()) as db:
        rows = db.exec(select(ControlCommand).where(ControlCommand.state == "pending",
            ControlCommand.next_attempt_at <= now()).limit(5).with_for_update(skip_locked=True)).all()
        for row in rows:
            try:
                if row.kind.startswith("group_state:"):
                    path = "/v1/service/groups/state"
                    payload = {"tenant_id": row.tenant_id, "conversation_id": row.binding_id,
                               "state": row.kind.split(":", 1)[1], "binding_generation": row.binding_generation}
                elif row.kind.startswith("tenant_state:"):
                    path = f"/v1/service/tenants/{row.tenant_id}/state"
                    payload = {"state": row.kind.split(":", 1)[1]}
                else:
                    path = "/v1/service/groups/activate"
                    payload = {"tenant_id": row.tenant_id, "conversation_id": row.binding_id,
                               "claim_id": row.claim_id, "binding_generation": row.binding_generation}
                response = client.post(settings().control_url.rstrip("/") + path,
                    headers=_control_headers(), json=payload, timeout=20)
                response.raise_for_status()
                row.state = "delivered"
            except httpx.HTTPError:
                row.attempts += 1
                row.next_attempt_at = now() + backoff(row.attempts)
            db.add(row)
        db.commit()


def retain() -> None:
    with Session(engine()) as db:
        cutoff = now() - 86400
        dead = now() - 7*86400
        for model in (InboxEvent, Outbox):
            rows = db.exec(select(model).where(model.payload_enc.is_not(None),
                or_(and_(model.completed_at.is_not(None), model.completed_at < cutoff),
                    model.created_at < dead,
                    and_(model.expires_at.is_not(None), model.expires_at <= now()))).with_for_update(skip_locked=True)).all()
            for row in rows:
                row.payload_enc = None
                if model is Outbox and row.state == "dispatching":
                    # Keep the in-flight lease: a matching response may still
                    # establish acceptance; expiry cannot prove it was unsent.
                    row.state, row.completed_at, row.error_code = "uncertain", now(), "payload_expired_during_send"
                elif row.state in {"queued", "claimed", "pending"}:
                    if model is Outbox and row.state == "claimed":
                        account = db.get(ProviderAccount, settings().provider_account_id)
                        if account and account.send_owner == row.lease_owner:
                            account.send_owner, account.send_lease_until = None, 0
                            db.add(account)
                    if model is InboxEvent and row.binding_id:
                        binding = db.get(Binding, row.binding_id)
                        if binding and binding.processing_event_id == row.id:
                            binding.processing_event_id, binding.processing_lease_until = None, 0
                            db.add(binding)
                    row.state, row.completed_at, row.error_code = "expired", now(), "payload_retention_expired"
                db.add(row)
        for row in db.exec(select(ShareApproval).where(ShareApproval.expires_at < now(),
                ShareApproval.state.not_in(["expired", "revoked"])).with_for_update(skip_locked=True)):
            row.summary_enc, row.result_enc = seal("expired", "share-summary:"+row.id), None
            row.state = "expired"
            db.add(row)
        for row in db.exec(select(GroupClaim).where(GroupClaim.expires_at < cutoff,
                GroupClaim.state.not_in(["expired", "revoked"])).with_for_update(skip_locked=True)):
            row.nonce_enc, row.group_enc = seal("expired", "claim:"+row.id), None
            row.state = "expired"
            db.add(row)
        for row in db.exec(select(PhoneChallenge).where(PhoneChallenge.expires_at < cutoff,
                PhoneChallenge.code_hash != "expired").with_for_update(skip_locked=True)):
            row.address_enc = seal("expired", "challenge-address:"+row.id)
            row.fallback_enc = seal("expired", "challenge-fallback:"+row.id)
            row.code_hash, row.fallback_hash = "expired", digest("expired", row.id)
            db.add(row)
        db.commit()


def tick(client: httpx.Client) -> int:
    recover_leases()
    claims = [c for _ in range(settings().dispatch_concurrency) if (c := claim_inbound())]
    with concurrent.futures.ThreadPoolExecutor(max_workers=settings().dispatch_concurrency) as pool:
        list(pool.map(lambda claim: dispatch(claim, client), claims))
    outgoing = claim_outbound()
    if outgoing:
        send(outgoing, client)
    deliver_control_commands(client)
    return len(claims) + bool(outgoing)


def main() -> None:
    settings().check_secrets()
    verify_schema()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    def inbound_lane():
        with httpx.Client(follow_redirects=False) as client:
            while not stop.is_set():
                try:
                    if claim := claim_inbound():
                        dispatch(claim, client)
                    else:
                        stop.wait(.25)
                except Exception:
                    log.error("inbound lane failed; committed work remains queued")
                    stop.wait(1)

    def outbound_lane():
        with httpx.Client(follow_redirects=False) as client:
            while not stop.is_set():
                try:
                    if claim := claim_outbound():
                        send(claim, client)
                    else:
                        stop.wait(.25)
                except Exception:
                    log.error("outbound lane failed; committed work remains queued")
                    stop.wait(1)

    # Slow group metadata/admission calls cannot block challenge or reply sends.
    with concurrent.futures.ThreadPoolExecutor(max_workers=settings().dispatch_concurrency+1) as pool:
        futures = [pool.submit(inbound_lane) for _ in range(settings().dispatch_concurrency)]
        futures.append(pool.submit(outbound_lane))
        with httpx.Client(follow_redirects=False) as client:
            last_retention = 0
            while not stop.is_set():
                try:
                    recover_leases()
                    deliver_control_commands(client)
                    if now() - last_retention > 300:
                        retain()
                        last_retention = now()
                except Exception:
                    log.error("maintenance lane failed; committed work remains queued")
                stop.wait(.5)


if __name__ == "__main__":
    main()
