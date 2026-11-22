"""Attachment bytes come only from the fixed, scoped broker capability."""
import hashlib
import os
import re
import tempfile
import time
from pathlib import Path
from gateway.platforms.event import MessageType
from .client import BrokerError
from .store import expired

EXTENSIONS={"image/jpeg":".jpg","image/png":".png","image/webp":".webp","image/gif":".gif","audio/ogg":".ogg","audio/mpeg":".mp3","audio/mp4":".m4a","video/mp4":".mp4","application/pdf":".pdf","text/plain":".txt","text/csv":".csv"}

def cleanup(scope):
    directory=scope.home/".squire"/"media"
    if not directory.exists():return
    now=time.time()
    for path in directory.iterdir():
        try:
            if not path.is_file():continue
            match=re.fullmatch(r"[0-9a-f]{64}\.([0-9]+)\.[a-z0-9]+",path.name)
            temporary=re.fullmatch(r"\.download-([0-9]+)-.+",path.name)
            if match:deadline=int(match[1])
            elif temporary:deadline=int(temporary[1])
            else:deadline=0  # Legacy cache files have lost the provider deadline.
            if now>=deadline:path.unlink(missing_ok=True)
        except FileNotFoundError:pass


def attach(broker,scope,event,descriptor):
    if not isinstance(descriptor,dict):raise BrokerError(422)
    expires_at=event.metadata.get("squire_expires_at",0)
    if expired(expires_at):raise BrokerError(410)
    body,mime=broker.fetch_media(event.message_id)
    if expired(expires_at):raise BrokerError(410)
    directory=scope.home/".squire"/"media";directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    # Provider filenames and paths never select a filesystem location.
    deadline=min(int(time.time())+86400,expires_at) if expires_at is not None else int(time.time())+86400
    # The filename keeps the deadline across restarts without a sidecar race.
    path=directory/(hashlib.sha256(event.message_id.encode()).hexdigest()+"."+str(deadline)+EXTENSIONS.get(mime,".bin"))
    fd,temp=tempfile.mkstemp(prefix=f".download-{min(deadline,int(time.time())+3600)}-",dir=directory)
    try:
        with os.fdopen(fd,"wb") as stream:
            stream.write(body);stream.flush();os.fsync(stream.fileno())
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)
    cleanup(scope)
    if expired(expires_at):path.unlink(missing_ok=True);raise BrokerError(410)
    kind=str(descriptor.get("type") or "").lower()
    event.message_type=(MessageType.VOICE if kind in {"ptt","voice","audio"} and mime.startswith("audio/") else MessageType.PHOTO if mime.startswith("image/") else MessageType.AUDIO if mime.startswith("audio/") else MessageType.VIDEO if mime.startswith("video/") else MessageType.DOCUMENT)
    event.media_urls=[str(path)];event.media_types=[mime];event.media_text_inlined=[False]
