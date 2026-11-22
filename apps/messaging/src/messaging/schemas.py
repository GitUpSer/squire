from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Participant(StrictModel):
    jid: str = Field(min_length=3, max_length=150)
    phone_jid: str | None = Field(default=None, max_length=150)
    admin: bool = False


class GroupSnapshot(StrictModel):
    fetched_at: float
    participants: list[Participant] = Field(max_length=2000)
    jid: str | None = None
    owner_jid: str | None = None


class Event(StrictModel):
    event_id: str = Field(min_length=1, max_length=200)
    sequence: int = Field(ge=0)
    provider_message_id: str = Field(min_length=1, max_length=200)
    chat_jid: str = Field(min_length=3, max_length=150)
    sender_jid: str = Field(default="", max_length=150)
    chat_phone_jid: str | None = Field(default=None, max_length=150)
    sender_phone_jid: str | None = Field(default=None, max_length=150)
    kind: Literal["message", "receipt", "identity_move", "delete"] = "message"
    chat_type: Literal["dm", "group", "other"] = "dm"
    origin: Literal["live", "replay", "history", "unknown"] = "unknown"
    timestamp: float
    expires_at: int | None = Field(default=None, ge=1, le=253402300799, strict=True)
    from_me: bool = False
    text: str = Field(default="", max_length=32000)
    reply_to: str | None = Field(default=None, max_length=200)
    mentions: list[str] = Field(default_factory=list, max_length=100)
    receipt_status: Literal["delivered", "read", "played"] | None = None
    recipient_jid: str | None = Field(default=None, max_length=150)
    new_chat_jid: str | None = Field(default=None, max_length=150)
    group: GroupSnapshot | None = None
    media: dict | None = None

    @model_validator(mode="after")
    def expiry_known(self):
        if self.kind == "message" and "expires_at" not in self.model_fields_set:
            raise ValueError("message expiry must be explicitly known")
        return self


class IngestBatch(StrictModel):
    provider_account_id: str = Field(min_length=1, max_length=100)
    store_id: str = Field(min_length=1, max_length=200)
    checkpoint_before: str | None = Field(default=None, max_length=4096)
    checkpoint_after: str = Field(min_length=1, max_length=4096)
    events: list[Event] = Field(max_length=100)


class ChallengeRequest(StrictModel):
    operation_id: str = Field(min_length=8, max_length=200)
    tenant_id: str = Field(min_length=1, max_length=100)
    account_id: str = Field(min_length=1, max_length=100)
    phone: str = Field(min_length=8, max_length=24)
    consent_version: str = Field(min_length=1, max_length=100)


class VerifyChallenge(StrictModel):
    account_id: str = Field(min_length=1, max_length=100)
    challenge_id: str = Field(min_length=1, max_length=100)
    code: str = Field(min_length=8, max_length=8, pattern=r"^[0-9]{8}$")


class RuntimeRegistration(StrictModel):
    runtime_id: str = Field(min_length=1, max_length=100)
    tenant_id: str = Field(min_length=1, max_length=100)
    conversation_id: str = Field(min_length=1, max_length=100)
    kind: Literal["dm", "group"]
    endpoint: str = Field(max_length=500)
    ingress_token: str = Field(min_length=32, max_length=500)
    egress_token: str = Field(min_length=32, max_length=500)


class StateRequest(StrictModel):
    state: Literal["verifying", "provisioning", "ready_unstarted", "active", "imported_active", "paused", "expired", "deleted"]
    expires_at: float | None = None
    group_limit: int | None = Field(default=None, ge=1, le=100)


class ClaimRequest(StrictModel):
    tenant_id: str = Field(min_length=1, max_length=100)
    operation_id: str = Field(min_length=8, max_length=200)


class GroupSettings(StrictModel):
    tenant_id: str = Field(min_length=1, max_length=100)
    active: bool | None = None
    mode: Literal["addressed", "all"] | None = None


class OutboundMedia(StrictModel):
    data_base64: str = Field(min_length=4, max_length=13981016)
    mime_type: str = Field(min_length=3, max_length=100)
    filename: str = Field(min_length=1, max_length=120)
    media_type: Literal["image", "audio", "video", "document"]
    ptt: bool = False

    @model_validator(mode="after")
    def bounded_attachment(self):
        import base64
        import binascii
        import re
        try:
            content = base64.b64decode(self.data_base64, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("invalid attachment encoding") from None
        if not 1 <= len(content) <= 10 * 1024 * 1024:
            raise ValueError("attachment exceeds pilot size limit")
        if not re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", self.mime_type):
            raise ValueError("invalid attachment type")
        if self.media_type != "document" and not self.mime_type.startswith(self.media_type + "/"):
            raise ValueError("attachment type mismatch")
        if self.ptt and (self.media_type != "audio" or self.mime_type != "audio/ogg"):
            raise ValueError("voice notes require Ogg audio")
        if "/" in self.filename or "\\" in self.filename or any(ord(c) < 32 for c in self.filename):
            raise ValueError("attachment filename must be a plain filename")
        return self


class OutboundRequest(StrictModel):
    conversation_id: str = Field(min_length=1, max_length=100)
    response_key: str = Field(min_length=1, max_length=200)
    text: str = Field(default="", max_length=32000)
    media: OutboundMedia | None = None
    reply_to: str | None = Field(default=None, max_length=200)
    priority: Literal["interactive", "scheduled"] = "interactive"
    cause_event_id: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def content_required(self):
        if not self.text and self.media is None:
            raise ValueError("message content is required")
        return self


class ProviderControl(StrictModel):
    paused: bool
    generation: int | None = Field(default=None, ge=1)


class ImportBinding(StrictModel):
    operation_id: str = Field(min_length=8, max_length=200)
    tenant_id: str = Field(min_length=1, max_length=100)
    account_id: str = Field(min_length=1, max_length=100)
    recipient: str = Field(min_length=3, max_length=150)
    aliases: list[str] = Field(default_factory=list, max_length=10)
    kind: Literal["dm", "group"] = "dm"
    activated_at: float = Field(ge=0, allow_inf_nan=False)
    handled_provider_message_ids: list[Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.:-]+$")]] = Field(default_factory=list, max_length=1000)
    owner_recipient: str | None = Field(default=None, max_length=150)
    group_limit: int = Field(default=1, ge=1, le=100)
    group_mode: Literal["addressed", "all"] = "addressed"

    @model_validator(mode="after")
    def group_mode_scope(self):
        if self.kind != "group" and self.group_mode != "addressed":
            raise ValueError("all-message mode requires a group import")
        return self


class ShareRequest(StrictModel):
    request_key: str = Field(min_length=8, max_length=200)
    summary: str = Field(min_length=1, max_length=2000)


class ShareDecision(StrictModel):
    tenant_id: str = Field(min_length=1, max_length=100)
    decision: Literal["approve", "deny", "release"]
    result: str | None = Field(default=None, max_length=32000)
