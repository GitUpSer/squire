"""Passwordless sessions and capability-specific runtime credentials."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from datetime import timedelta
from fastapi import Depends, HTTPException, Request
from sqlalchemy import text
from sqlmodel import Session, select
from control_api.config import get_settings
from control_api.db import get_session
from control_api.models import utcnow
from control_api.pilot_models import PilotAccount, PilotSession, PilotRuntime
from control_api.provisioning import as_aware

COOKIE = "squire_session"


def secret() -> str:
    value = get_settings().pilot_secret
    if len(value) < 32:
        raise HTTPException(503, "pilot security is not configured")
    return value


def digest(kind: str, value: str) -> str:
    return hmac.new(secret().encode(), f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()


def runtime_token(runtime_id: str, purpose: str, generation: int = 1) -> str:
    return digest("runtime", f"{purpose}:{runtime_id}:{generation}")


def bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "authentication required")
    return token


def require_messaging_service(request: Request):
    expected = get_settings().control_messaging_service_token
    token = bearer(request)
    if not expected or not hmac.compare_digest(expected, token):
        raise HTTPException(401, "invalid service credential")


def require_runtime(request: Request, session: Session, runtime_id: str) -> PilotRuntime:
    runtime = session.get(PilotRuntime, runtime_id)
    if runtime is None or runtime.state in {"deleted", "paused", "expired", "revoking"}:
        raise HTTPException(403, "runtime unavailable")
    if not hmac.compare_digest(bearer(request), runtime_token(runtime.id, "control", runtime.generation)):
        raise HTTPException(403, "runtime credential does not authorize this resource")
    return runtime


def pilot_available():
    if not get_settings().pilot_enabled:
        raise HTTPException(503, "pilot is not open")
    secret()


def check_origin(request: Request):
    if request.headers.get("origin") != get_settings().pilot_web_origin.rstrip("/"):
        raise HTTPException(403, "invalid request origin")


def current_account(request: Request, session: Session = Depends(get_session)) -> PilotAccount:
    pilot_available()
    token = request.cookies.get(COOKIE, "")
    if not token:
        raise HTTPException(401, "sign in required")
    row = session.get(PilotSession, digest("session", token))
    if row is None or row.revoked or as_aware(row.expires_at) <= utcnow():
        raise HTTPException(401, "session expired")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        check_origin(request)
        csrf = request.headers.get("x-csrf-token", "")
        if not csrf or not hmac.compare_digest(row.csrf_digest, digest("csrf", csrf)):
            raise HTTPException(403, "invalid request token")
    account = session.get(PilotAccount, row.account_id)
    if account is None or account.state == "deleted":
        raise HTTPException(401, "account unavailable")
    return account


def limit(session: Session, key: str, maximum: int, seconds: int = 3600):
    """Atomic database fixed-window counter; shared by every API replica."""
    window = int(time.time()) // seconds
    bucket = digest("rate", f"{key}:{window}")
    expires = utcnow() + timedelta(seconds=seconds)
    hits = session.execute(text(
        "INSERT INTO pilotratebucket (id,hits,expires_at) VALUES (:id,1,:expiry) "
        "ON CONFLICT (id) DO UPDATE SET hits=pilotratebucket.hits+1 RETURNING hits"
    ), {"id": bucket, "expiry": expires}).scalar_one()
    # Persist even rejected attempts and later route failures.
    session.commit()
    if hits > maximum:
        raise HTTPException(429, "too many attempts; try again later")


def issue_session(session: Session, account_id: str) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    csrf = digest("csrf-derive", token)
    session.add(PilotSession(digest=digest("session", token), account_id=account_id,
                            csrf_digest=digest("csrf", csrf), expires_at=utcnow()+timedelta(days=7)))
    session.commit()
    return token, csrf
