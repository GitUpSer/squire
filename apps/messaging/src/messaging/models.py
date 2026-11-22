from sqlalchemy import BigInteger, Column, Text, UniqueConstraint
from sqlmodel import SQLModel, Field


class SchemaRevision(SQLModel, table=True):
    __tablename__ = "schema_revision"
    id: int = Field(primary_key=True)
    version: int


class ProviderAccount(SQLModel, table=True):
    __tablename__ = "provider_account"
    id: str = Field(primary_key=True)
    generation: int = 1
    paused: bool = True
    last_sent_at: float = 0
    send_owner: str | None = None
    send_lease_until: float = 0


class TenantState(SQLModel, table=True):
    __tablename__ = "tenant_state"
    tenant_id: str = Field(primary_key=True)
    account_id: str
    state: str = "verifying"
    expires_at: float | None = None
    opted_out: bool = False
    group_limit: int = 1
    last_dispatched_at: float = 0
    last_sent_at: float = 0


class Binding(SQLModel, table=True):
    __tablename__ = "binding"
    id: str = Field(primary_key=True)
    provider_account_id: str = Field(index=True)
    tenant_id: str = Field(index=True)
    address_hash: str = Field(unique=True, index=True)
    address_enc: str = Field(sa_column=Column(Text, nullable=False))
    kind: str
    active: bool = True
    generation: int = 1
    runtime_id: str | None = Field(default=None, index=True)
    activated_at: float
    mode: str = "addressed"
    owner_identity_hash: str | None = None
    admin_identity_hash: str | None = None
    membership_checked_at: float = 0
    owner_present: bool = True
    processing_event_id: str | None = None
    processing_lease_until: float = 0


class IdentityAlias(SQLModel, table=True):
    __tablename__ = "identity_alias"
    address_hash: str = Field(primary_key=True)
    binding_id: str = Field(index=True)
    evidence: str


class Runtime(SQLModel, table=True):
    __tablename__ = "runtime"
    id: str = Field(primary_key=True)
    tenant_id: str = Field(index=True)
    binding_id: str = Field(unique=True)
    kind: str
    endpoint: str
    ingress_token_enc: str = Field(sa_column=Column(Text, nullable=False))
    egress_token_hash: str = Field(unique=True, index=True)
    active: bool = True
    generation: int = 1


class PhoneChallenge(SQLModel, table=True):
    __tablename__ = "phone_challenge"
    id: str = Field(primary_key=True)
    operation_id: str = Field(unique=True)
    account_id: str = Field(index=True)
    tenant_id: str = Field(index=True)
    address_hash: str = Field(index=True)
    address_enc: str = Field(sa_column=Column(Text, nullable=False))
    code_hash: str
    fallback_hash: str
    fallback_enc: str = Field(sa_column=Column(Text, nullable=False))
    expires_at: float
    created_at: float
    attempts: int = 0
    consumed_at: float | None = None
    binding_id: str | None = None
    outbox_id: str
    consent_version: str


class GroupClaim(SQLModel, table=True):
    __tablename__ = "group_claim"
    id: str = Field(primary_key=True)
    operation_id: str = Field(unique=True)
    tenant_id: str = Field(index=True)
    nonce_hash: str = Field(unique=True, index=True)
    nonce_enc: str = Field(sa_column=Column(Text, nullable=False))
    expires_at: float
    state: str = "pending"
    group_hash: str | None = None
    group_enc: str | None = Field(default=None, sa_column=Column(Text))
    binding_id: str | None = None
    owner_hash: str | None = None
    administrator_hash: str | None = None


class InboxEvent(SQLModel, table=True):
    __tablename__ = "inbox_event"
    id: str = Field(primary_key=True)
    logical_key: str = Field(unique=True, index=True)
    provider_account_id: str
    provider_message_id: str = Field(index=True)
    binding_id: str | None = Field(default=None, index=True)
    tenant_id: str | None = Field(default=None, index=True)
    kind: str
    state: str = Field(default="pending", index=True)
    payload_enc: str | None = Field(default=None, sa_column=Column(Text))
    created_at: float = Field(index=True)
    expires_at: int | None = Field(default=None, sa_type=BigInteger)
    next_attempt_at: float = 0
    attempts: int = 0
    lease_owner: str | None = None
    lease_until: float = 0
    fence: int = 0
    error_code: str | None = None
    completed_at: float | None = None


class Observation(SQLModel, table=True):
    __tablename__ = "provider_observation"
    __table_args__ = (UniqueConstraint("store_id", "sequence"),)
    id: str = Field(primary_key=True)
    store_id: str = Field(index=True)
    sequence: int = Field(sa_type=BigInteger)
    event_id: str
    created_at: float


class Checkpoint(SQLModel, table=True):
    __tablename__ = "provider_checkpoint"
    store_id: str = Field(primary_key=True)
    provider_account_id: str
    checkpoint: str | None = None
    updated_at: float


class Outbox(SQLModel, table=True):
    __tablename__ = "outbox_message"
    id: str = Field(primary_key=True)
    response_key: str = Field(unique=True, index=True)
    tenant_id: str = Field(index=True)
    runtime_id: str | None = None
    binding_id: str | None = Field(default=None, index=True)
    binding_generation: int | None = None
    challenge_id: str | None = None
    cause_event_id: str | None = Field(default=None, index=True)
    payload_enc: str | None = Field(default=None, sa_column=Column(Text))
    state: str = Field(default="queued", index=True)
    priority: int = 0
    created_at: float = Field(index=True)
    expires_at: float
    next_attempt_at: float = 0
    attempts: int = 0
    lease_owner: str | None = None
    lease_until: float = 0
    fence: int = 0
    provider_message_id: str | None = Field(default=None, index=True)
    error_code: str | None = None
    completed_at: float | None = None


class Receipt(SQLModel, table=True):
    __tablename__ = "delivery_event"
    id: str = Field(primary_key=True)
    outbox_id: str = Field(index=True)
    recipient_hash: str
    status: str
    timestamp: float


class ControlCommand(SQLModel, table=True):
    __tablename__ = "control_command_outbox"
    id: str = Field(primary_key=True)
    kind: str
    tenant_id: str
    binding_id: str
    claim_id: str
    binding_generation: int = 1
    state: str = Field(default="pending", index=True)
    attempts: int = 0
    next_attempt_at: float = 0


class ShareApproval(SQLModel, table=True):
    __tablename__ = "share_approval"
    id: str = Field(primary_key=True)
    tenant_id: str = Field(index=True)
    group_binding_id: str
    request_key: str = Field(unique=True)
    summary_enc: str = Field(sa_column=Column(Text, nullable=False))
    result_enc: str | None = Field(default=None, sa_column=Column(Text))
    state: str = "requested"
    created_at: float
    expires_at: float


class OptOut(SQLModel, table=True):
    __tablename__ = "recipient_opt_out"
    address_hash: str = Field(primary_key=True)
    created_at: float
