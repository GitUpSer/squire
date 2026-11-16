"""Event-scoped attachment relay; provider addresses never leave messaging."""
import re
import time
import threading
from contextlib import contextmanager
import httpx
from fastapi import HTTPException, Response
from .config import settings
from .crypto import open_sealed
from .models import Binding, InboxEvent, Runtime, TenantState
from .service import eligible, now

MAX_BYTES = 10 * 1024 * 1024
MAX_AGE_SECONDS = 24 * 3600
_fetch_lock = threading.Lock()
_fetching = set()


@contextmanager
def fetch_slot(runtime_id):
    # Bound buffered bytes and slow downloads without queuing DB connections.
    with _fetch_lock:
        if runtime_id in _fetching or len(_fetching) >= 4:
            raise HTTPException(429, "attachment delivery busy; retry shortly")
        _fetching.add(runtime_id)
    try:
        yield
    finally:
        with _fetch_lock:
            _fetching.remove(runtime_id)


def broker_client():
    return httpx.Client(timeout=httpx.Timeout(45, connect=5), trust_env=False)


def authorized_event(db, runtime_id, event_id):
    runtime = db.get(Runtime, runtime_id)
    row = db.get(InboxEvent, event_id)
    binding = db.get(Binding, runtime.binding_id) if runtime else None
    tenant = db.get(TenantState, runtime.tenant_id) if runtime else None
    if (not runtime or not runtime.active or not binding or not binding.active
            or binding.runtime_id != runtime.id or not tenant or not eligible(tenant)
            or not row or row.binding_id != binding.id or row.tenant_id != runtime.tenant_id
            or row.kind != "message" or row.state not in {"pending", "claimed", "done"}
            or not row.payload_enc):
        raise HTTPException(404, "attachment unavailable")
    payload = open_sealed(row.payload_enc, "inbox:" + row.id)
    if (not payload.get("media") or payload.get("from_me")
            or payload.get("origin") not in {"live", "replay"}
            or payload.get("timestamp", 0) < binding.activated_at
            or min(row.created_at, payload.get("timestamp", 0)) + MAX_AGE_SECONDS < now()):
        raise HTTPException(404, "attachment unavailable")
    return row, payload


def fetch_media(db, runtime, event_id):
    runtime_id = runtime.id
    row, payload = authorized_event(db, runtime_id, event_id)
    cfg = settings()
    if not cfg.transport_url or not cfg.transport_broker_token:
        raise HTTPException(503, "attachment delivery unavailable")
    request = {"provider_account_id": row.provider_account_id,
               "chat_jid": payload["chat_jid"], "provider_message_id": row.provider_message_id}
    # Both authentication and authorization are reads. Release their transaction
    # before transport I/O so STOP/revocation can use the database pool.
    db.rollback()
    with fetch_slot(runtime_id):
        content, mime = download(cfg, request)
    authorized_event(db, runtime_id, event_id)
    return Response(content, media_type=mime,
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


def download(cfg, request):
    chunks, size, started = [], 0, time.monotonic()
    try:
        with broker_client() as client:
            with client.stream("POST", cfg.transport_url.rstrip("/") + "/v1/media/fetch",
                               headers={"Authorization": "Bearer " + cfg.transport_broker_token},
                               json=request) as response:
                if response.status_code in {404, 409, 410, 415}:
                    raise HTTPException(404, "attachment unavailable")
                if response.status_code == 413:
                    raise HTTPException(413, "attachment exceeds pilot size limit")
                response.raise_for_status()
                declared = int(response.headers.get("content-length", "0"))
                if declared < 0 or declared > MAX_BYTES:
                    raise HTTPException(413, "attachment exceeds pilot size limit")
                mime = response.headers.get("content-type", "application/octet-stream").split(";", 1)[0].lower()
                if not re.fullmatch(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", mime):
                    mime = "application/octet-stream"
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise HTTPException(413, "attachment exceeds pilot size limit")
                    if time.monotonic() - started > 60:
                        raise HTTPException(503, "attachment delivery unavailable")
                    chunks.append(chunk)
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, "attachment delivery unavailable") from None
    return b"".join(chunks), mime
