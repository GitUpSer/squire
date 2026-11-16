"""Pilot control metadata. Addresses, messages and provider secrets live elsewhere."""
from __future__ import annotations

from datetime import datetime
from sqlmodel import SQLModel, Field
from sqlalchemy import DateTime
from control_api.models import utcnow


class PilotAccount(SQLModel, table=True):
    id: str = Field(primary_key=True)
    email: str = Field(unique=True, index=True)
    tenant_id: str | None = Field(default=None, unique=True, index=True)
    display_name: str = ""
    timezone: str = "UTC"
    state: str = "verified"
    consent_version: str | None = None
    consent_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))


class PilotAuthChallenge(SQLModel, table=True):
    id: str = Field(primary_key=True)
    email: str = Field(index=True)
    digest: str
    expires_at: datetime = Field(sa_type=DateTime(timezone=True))
    attempts: int = 0
    consumed: bool = False
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))


class PilotSession(SQLModel, table=True):
    digest: str = Field(primary_key=True)
    account_id: str = Field(foreign_key="pilotaccount.id", index=True)
    csrf_digest: str
    expires_at: datetime = Field(sa_type=DateTime(timezone=True))
    revoked: bool = False
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))


class PilotInvitation(SQLModel, table=True):
    id: str = Field(primary_key=True)
    digest: str = Field(unique=True, index=True)
    email: str = Field(index=True)
    expires_at: datetime = Field(sa_type=DateTime(timezone=True))
    account_id: str | None = Field(default=None, index=True)
    state: str = "available"
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))


class PilotCapacity(SQLModel, table=True):
    id: str = Field(primary_key=True, default="pilot")
    revision: int = 0


class PilotAdmission(SQLModel, table=True):
    account_id: str = Field(primary_key=True, foreign_key="pilotaccount.id")
    invitation_id: str | None = Field(default=None, unique=True)
    state: str = "reserved"
    reserved_until: datetime = Field(sa_type=DateTime(timezone=True))
    accepted_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))


class PilotRuntime(SQLModel, table=True):
    id: str = Field(primary_key=True, foreign_key="tenant.id")
    owner_tenant_id: str = Field(index=True)
    scope_id: str = Field(unique=True, index=True)
    kind: str = "dm"
    generation: int = 1
    ready: bool = False
    registered: bool = False
    model_ready: bool = False
    deployment_generation: int = 0
    scope_generation: int = 1
    state: str = "provisioning"
    last_seen_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))


class PilotEntitlement(SQLModel, table=True):
    tenant_id: str = Field(primary_key=True, foreign_key="tenant.id")
    state: str = "ready_unstarted"
    activated_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    expires_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    daily_index: int = 0
    daily_turns: int = 0
    lifetime_turns: int = 0
    revision: int = 0
    model_budget_cents: int = 1000
    extension_requested: bool = False
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))


class PilotTurn(SQLModel, table=True):
    id: str = Field(primary_key=True)
    tenant_id: str = Field(index=True)
    runtime_id: str
    admitted_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))


class PilotOperation(SQLModel, table=True):
    id: str = Field(primary_key=True)
    account_id: str = Field(index=True)
    kind: str
    reference: str | None = None
    state: str = "pending"
    attempts: int = 0
    next_attempt_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))
    lease_until: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    lease_generation: int = 0
    last_code: str | None = None
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))


class PilotRateBucket(SQLModel, table=True):
    id: str = Field(primary_key=True)
    hits: int = 0
    expires_at: datetime = Field(sa_type=DateTime(timezone=True))


class PilotSchemaRevision(SQLModel, table=True):
    id: int = Field(primary_key=True)
    applied_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))


class PilotExtension(SQLModel, table=True):
    """Operator decision metadata only; no conversation content or credentials."""
    id: str = Field(primary_key=True)
    tenant_id: str = Field(index=True)
    operator_label: str
    reason_code: str
    previous_expires_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    expires_at: datetime = Field(sa_type=DateTime(timezone=True))
    previous_budget_cents: int
    budget_cents: int
    state: str = "approved"
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))
    applied_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
