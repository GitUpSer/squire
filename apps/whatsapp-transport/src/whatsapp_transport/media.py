"""Bounded, read-only media fetches from authenticated provider records."""
import asyncio
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .normalize import JID

MAX_MEDIA_BYTES = 10 * 1024 * 1024
TYPES = {"image", "audio", "video", "document"}


class FetchMedia(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_account_id: str = Field(min_length=1, max_length=100)
    chat_jid: str = Field(min_length=1, max_length=100)
    provider_message_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.:-]+$")


def safe_filename(value):
    return re.sub(r"[^A-Za-z0-9._ -]", "_", str(value or "attachment"))[-120:] or "attachment"


def safe_mime(value, kind):
    value = str(value or "").split(";", 1)[0].lower().strip()
    if not re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", value):
        return "application/octet-stream"
    if kind in {"audio", "image", "video"} and not value.startswith(kind + "/"):
        return "application/octet-stream"
    return value


class Media:
    def __init__(self, settings, cli):
        self.settings, self.cli = settings, cli
        # Upstream bounds its encrypted download too. One process at a time
        # keeps malformed provider metadata from multiplying its memory budget.
        self.lock = asyncio.Lock()

    def metadata(self, request):
        if request.provider_account_id != self.settings.provider_account_id or not JID.fullmatch(request.chat_jid):
            raise HTTPException(404, "media_not_found")
        path = self.settings.store_dir / "wacli.db"
        if not path.is_file():
            raise HTTPException(404, "media_not_found")
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            row = db.execute("""SELECT media_type,mime_type,filename,file_length
                FROM messages WHERE chat_jid=? AND msg_id=? AND COALESCE(revoked,0)=0
                AND COALESCE(deleted_for_me,0)=0 AND COALESCE(deleted_at,0)=0
                AND COALESCE(payload_purged_at,0)=0""", (request.chat_jid, request.provider_message_id)).fetchone()
        if not row:
            raise HTTPException(404, "media_not_found")
        kind, mime, filename, size = row
        if kind not in TYPES:
            raise HTTPException(415, "unsupported_media")
        if not isinstance(size, int) or not 0 < size <= MAX_MEDIA_BYTES:
            raise HTTPException(413, "media_size_unavailable_or_exceeded")
        return {"type": kind, "mime_type": safe_mime(mime, kind), "filename": safe_filename(filename), "size": size}

    async def fetch(self, request):
        async with self.lock:
            metadata = self.metadata(request)
            root = Path(tempfile.mkdtemp(prefix="squire-media-"))
            target = root / "payload"
            try:
                await self.cli.run("media", "download", "--chat", request.chat_jid,
                                   "--id", request.provider_message_id, "--output", str(target), readonly=True)
                if target.is_symlink() or not target.is_file() or target.stat().st_size != metadata["size"]:
                    raise HTTPException(502, "media_size_mismatch")
                # A revoke/purge during download must not disclose a stale payload.
                if self.metadata(request) != metadata:
                    raise HTTPException(409, "media_changed")
                return root, target, metadata
            except BaseException:
                shutil.rmtree(root)
                raise
