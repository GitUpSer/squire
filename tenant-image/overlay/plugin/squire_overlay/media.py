"""Attachment bytes come only from the fixed, scoped broker capability."""
import hashlib
import os
import tempfile
import time
from pathlib import Path
from gateway.platforms.event import MessageType
from .client import BrokerError

EXTENSIONS={"image/jpeg":".jpg","image/png":".png","image/webp":".webp","image/gif":".gif","audio/ogg":".ogg","audio/mpeg":".mp3","audio/mp4":".m4a","video/mp4":".mp4","application/pdf":".pdf","text/plain":".txt","text/csv":".csv"}


def attach(broker,scope,event,descriptor):
    if not isinstance(descriptor,dict):raise BrokerError(422)
    body,mime=broker.fetch_media(event.message_id)
    directory=scope.home/".squire"/"media";directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    # Provider filenames and paths never select a filesystem location.
    path=directory/(hashlib.sha256(event.message_id.encode()).hexdigest()+EXTENSIONS.get(mime,".bin"))
    fd,temp=tempfile.mkstemp(prefix=".download-",dir=directory)
    try:
        with os.fdopen(fd,"wb") as stream:
            stream.write(body);stream.flush();os.fsync(stream.fileno())
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)
    cutoff=time.time()-86400
    for old in directory.iterdir():
        if old!=path and old.is_file() and old.stat().st_mtime<cutoff:old.unlink()
    kind=str(descriptor.get("type") or "").lower()
    event.message_type=(MessageType.VOICE if kind in {"ptt","voice","audio"} and mime.startswith("audio/") else MessageType.PHOTO if mime.startswith("image/") else MessageType.AUDIO if mime.startswith("audio/") else MessageType.VIDEO if mime.startswith("video/") else MessageType.DOCUMENT)
    event.media_urls=[str(path)];event.media_types=[mime];event.media_text_inlined=[False]
