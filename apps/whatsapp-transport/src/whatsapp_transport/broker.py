import hashlib
import hmac
import json
import time
import base64
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cli import CLIError
from .ledger import Conflict
from .normalize import group_info, recipient
from .media import MAX_MEDIA_BYTES, safe_filename, safe_mime


class OutboundMedia(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data_base64: str = Field(min_length=4, max_length=13981016)
    mime_type: str = Field(min_length=3, max_length=100)
    filename: str = Field(min_length=1, max_length=120)
    media_type: Literal["image", "audio", "video", "document"]
    ptt: bool = False

    @model_validator(mode="after")
    def validate_payload(self):
        try:
            raw = base64.b64decode(self.data_base64, validate=True)
        except ValueError:
            raise ValueError("invalid_media_encoding") from None
        if not 0 < len(raw) <= MAX_MEDIA_BYTES:
            raise ValueError("invalid_media_size")
        mime = safe_mime(self.mime_type, self.media_type)
        if mime != self.mime_type or (self.media_type != "document" and not mime.startswith(self.media_type + "/")):
            raise ValueError("invalid_media_mime")
        if self.ptt and (self.media_type != "audio" or mime != "audio/ogg"):
            raise ValueError("voice_requires_ogg_opus")
        self.filename = safe_filename(self.filename)
        return self


class Send(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(min_length=8, max_length=200)
    provider_account_id: str = Field(min_length=1, max_length=100)
    generation: int = Field(ge=1)
    recipient: str = Field(min_length=3, max_length=150)
    kind: Literal["text", "media"]
    text: str = Field(default="", max_length=32000)
    media: OutboundMedia | None = None
    reply_to: str | None = Field(default=None, max_length=200)

    @field_validator("recipient")
    @classmethod
    def explicit_recipient(cls, value):
        return recipient(value)

    @model_validator(mode="after")
    def payload_matches_kind(self):
        if (self.kind == "media") != (self.media is not None) or (self.kind == "text" and not self.text):
            raise ValueError("invalid_send_kind")
        return self


class Broker:
    def __init__(self, settings, cli, ledger, supervisor, capture):
        self.settings, self.cli, self.ledger = settings, cli, ledger
        self.supervisor, self.capture = supervisor, capture

    def result(self, operation_id, row):
        return dict(operation_id=operation_id, status=row["state"],
                    provider_message_id=row.get("provider_message_id"), error_code=row.get("error_code"))

    async def send(self, request: Send):
        # The fence and dispatch intent are protected by the same exclusive queue as admin.
        async with self.supervisor.lock:
            if request.provider_account_id != self.settings.provider_account_id:
                raise Conflict("wrong_provider_account")
            if request.generation != self.ledger.get("generation"):
                raise Conflict("stale_generation")
            fingerprint = hmac.new(self.settings.broker_token.encode(),
                json.dumps(request.model_dump(), sort_keys=True).encode(), hashlib.sha256).hexdigest()
            with self.ledger.connection() as db:
                row = db.execute("SELECT * FROM sends WHERE operation_id=?", (request.operation_id,)).fetchone()
            if row:
                if row["fingerprint"] != fingerprint:
                    raise Conflict("operation_payload_changed")
                if row["state"] != "definitely_unsent":
                    return self.result(request.operation_id, dict(row))
            refusal = None
            if not self.ledger.get("outbound_enabled", False):
                refusal = "outbound_paused"
            elif not self.supervisor.connected or self.ledger.get("logged_out", False):
                refusal = "not_connected"
            elif not self.ledger.get("capture_enabled", False) or self.capture.error:
                refusal = "capture_not_ready"
            if refusal:
                return dict(operation_id=request.operation_id, status="definitely_unsent", error_code=refusal, retryable=True)
            if row:
                self.ledger.retry_unsent(request.operation_id)
            else:
                self.ledger.begin_send(request.operation_id, fingerprint, request.generation)
            dispatched = False
            try:
                with tempfile.TemporaryDirectory(prefix="squire-send-") as directory:
                    if request.media:
                        target = Path(directory) / "payload"
                        with target.open("xb") as file:
                            target.chmod(0o600)
                            file.write(base64.b64decode(request.media.data_base64, validate=True))
                        args = ["send", "file", "--to", request.recipient, "--file", str(target),
                                "--filename", request.media.filename, "--mime", request.media.mime_type,
                                "--caption", request.text, "--max-size-mb", "10", "--as", request.media.media_type]
                        if request.media.ptt:
                            args.append("--ptt")
                    else:
                        args = ["send", "text", "--to", request.recipient, "--message", request.text, "--no-preview"]
                    if request.reply_to:
                        args.extend(["--reply-to", request.reply_to])
                    dispatched = True
                    data = await self.cli.run(*args)
                if not isinstance(data, dict) or not data.get("sent") or not data.get("id"):
                    raise CLIError("send_ack_missing")
                state, message_id, error = "accepted", data["id"], None
            except CLIError as exc:
                state = "definitely_unsent" if exc.definitely_unsent else "uncertain"
                message_id, error = None, exc.code
            except OSError:
                state = "uncertain" if dispatched else "definitely_unsent"
                message_id, error = None, "local_io_failed"
            self.ledger.finish_send(request.operation_id, state, message_id, error)
            return dict(operation_id=request.operation_id, status=state, provider_message_id=message_id, error_code=error,
                        retryable=state == "definitely_unsent" and error in {"queue_deadline", "cli_unavailable"})

    async def group(self, group_jid):
        group_jid = recipient(group_jid)
        if not group_jid.endswith("@g.us"):
            raise ValueError("group_jid_required")
        if self.ledger.get("logged_out", False):
            raise CLIError("logged_out", definitely_unsent=True)
        async with self.supervisor.exclusive():
            raw = await self.cli.run("groups", "info", "--jid", group_jid)
        result = group_info(raw, time.time())
        if result["jid"] != group_jid or not result["participants"]:
            raise CLIError("invalid_group_snapshot")
        return result

    async def leave(self, group_jid):
        group_jid = recipient(group_jid)
        if not group_jid.endswith("@g.us"):
            raise ValueError("group_jid_required")
        async with self.supervisor.exclusive():
            return await self.cli.run("groups", "leave", "--jid", group_jid)
