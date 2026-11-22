"""Hermes public platform adapter backed by Squire's durable scoped transport."""
from __future__ import annotations
import asyncio
import contextvars
import hashlib
import json
import os
import time
from datetime import datetime,timezone
from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType, ProcessingOutcome
from .config import RuntimeConfig
from .store import Store,expired
from .client import Broker

_current_event=contextvars.ContextVar("squire_event",default=None)
_final_send=contextvars.ContextVar("squire_final_send",default=False)


def model_configured(config):
    if any(os.environ.get(key) for key in ("ANTHROPIC_API_KEY","OPENAI_API_KEY","OPENROUTER_API_KEY")):return True
    # Imported provider credentials remain in their existing isolated runtime.
    # Readiness here means configured, not a claim of a successful model request.
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider
        resolved=resolve_runtime_provider()
        return bool(resolved.get("api_key") or resolved.get("access_token"))
    except Exception:return False


class SquireAdapter(BasePlatformAdapter):
    supports_async_delivery=True
    supports_inchannel_continuable=True
    splits_long_messages=False

    def __init__(self,config):
        config.extra["group_sessions_per_user"]=False
        config.extra["thread_sessions_per_user"]=False
        super().__init__(config,Platform("squire"))
        self.scope=RuntimeConfig.from_env();self.store=Store(self.scope.database);self.broker=Broker(self.scope)
        self._tasks=[];self._completion=None;self._send_counts={}

    async def connect(self,*,is_reconnect=False):
        if self._running:return True
        self._running=True
        from .media import cleanup
        await asyncio.to_thread(cleanup,self.scope)
        self._model_ready=await asyncio.to_thread(model_configured,self.scope)
        for event_id in self.store.recover():
            deadline=self.store.event_expiry(event_id)
            if not expired(deadline):self.store.enqueue({"conversation_id":self.scope.conversation_id,"response_key":"recovery:"+event_id,"text":"My last task was interrupted and may have taken an action. I have paused queued work so I don’t repeat it. Please ask the pilot operator to reconcile this task before continuing.","priority":"interactive","cause_event_id":event_id,"_expires_at":deadline})
        self.store.mark_runtime(self.scope.generation,True,self._model_ready)
        self._tasks=[asyncio.create_task(self._inbox_loop()),asyncio.create_task(self._outbox_loop()),asyncio.create_task(self._media_cleanup_loop())]
        if self.scope.kind=="dm":self._tasks.append(asyncio.create_task(self._approvals_loop()))
        return True

    async def disconnect(self):
        self._running=False;self.store.mark_runtime(self.scope.generation,False,False)
        for task in self._tasks:task.cancel()
        await asyncio.gather(*self._tasks,return_exceptions=True)
        # Executing turns remain running; next process marks them uncertain.

    async def _inbox_loop(self):
        while self._running:
            self.store.mark_runtime(self.scope.generation,True,self._model_ready)
            if not self._message_handler or not self._model_ready:
                await asyncio.sleep(1);continue
            envelope=self.store.claim()
            if not envelope:await asyncio.sleep(.25);continue
            if expired(envelope["payload"].get("expires_at",0)):
                self.store.cancel(envelope["event_id"]);continue
            if envelope["event_id"].startswith("approval:"):
                try:
                    authorized=await asyncio.to_thread(self.broker.request,"GET","/v1/approvals",timeout=3)
                    if envelope["event_id"][9:] not in {item["approval_id"] for item in authorized.get("approvals",[])}:
                        self.store.cancel(envelope["event_id"]);continue
                except Exception:
                    self.store.defer(envelope["event_id"]);await asyncio.sleep(3);continue
            payload=envelope["payload"]
            source=self.build_source(chat_id=self.scope.conversation_id,chat_name="Private Squire" if self.scope.kind=="dm" else "Approved group",chat_type=self.scope.kind,user_id=envelope["sender_id"],message_id=envelope["provider_message_id"])
            text=payload.get("text","")
            if self.scope.kind=="group" and text.lstrip().lower().startswith("!squire "):text=text.lstrip()[8:]
            event=MessageEvent(text=text,source=source,user_id=envelope["sender_id"],message_id=envelope["event_id"],reply_to_message_id=payload.get("reply_to"),timestamp=datetime.fromtimestamp(envelope["timestamp"],timezone.utc),metadata={"squire_event_id":envelope["event_id"],"squire_provider_message_id":envelope["provider_message_id"]},allow_gateway_control=self.scope.kind=="dm")
            event.metadata["squire_expires_at"]=payload["expires_at"]
            if envelope["event_id"].startswith("approval:"):event.metadata["squire_approval_id"]=envelope["event_id"][9:]
            token=_current_event.set(event);self._completion=asyncio.Event()
            try:
                if payload.get("media"):
                    from .media import attach
                    from .client import BrokerError
                    try:
                        await asyncio.to_thread(attach,self.broker,self.scope,event,payload["media"])
                    except BrokerError as exc:
                        if exc.status in {429,500,502,503,504}:
                            self.store.defer(envelope["event_id"]);await asyncio.sleep(3);continue
                        await self.send(source.chat_id,"I couldn’t retrieve that attachment. Please resend it; the pilot accepts files up to 10 MB and retrieves them within 24 hours.")
                        self.store.finish(envelope["event_id"],True);continue
                if expired(payload["expires_at"]):
                    self.store.cancel(envelope["event_id"]);continue
                await self.handle_message(event)
                if not event._gateway_accepted:
                    self.store.finish(envelope["event_id"],False)
                else:
                    await self._await_completion(event)
            except asyncio.CancelledError:raise
            except Exception:
                self.store.finish(envelope["event_id"],False)
            finally:
                _current_event.reset(token);self._completion=None

    async def _await_completion(self,event):
        # Ordinary turns stay FIFO. Owner control replies must reach Hermes while
        # an action waits for approval/clarification, or both sides deadlock.
        while not self._completion.is_set():
            if self.scope.kind=="dm":
                clarify=False
                try:
                    from tools.clarify_gateway import get_pending_for_session
                    clarify=get_pending_for_session(self._event_session_key(event),include_choice_prompts=True) is not None
                except ImportError:pass
                def is_control(envelope):
                    text=envelope["payload"].get("text","").strip()
                    command=text.split(maxsplit=1)[0].lower() if text else ""
                    return command in {"/approve","/deny","/stop","/cancel"} or clarify
                control=self.store.claim_control(is_control)
                if control:
                    if expired(control["payload"].get("expires_at",0)):
                        self.store.cancel(control["event_id"]);continue
                    source=self.build_source(chat_id=self.scope.conversation_id,chat_type="dm",user_id=control["sender_id"],message_id=control["provider_message_id"])
                    reply=MessageEvent(text=control["payload"].get("text",""),source=source,message_id=control["event_id"],metadata={"squire_event_id":control["event_id"],"squire_provider_message_id":control["provider_message_id"]})
                    reply.metadata["squire_expires_at"]=control["payload"]["expires_at"]
                    token=_current_event.set(reply)
                    try:
                        await self.handle_message(reply)
                        # The inline control path does not fire processing hooks.
                        if not reply._gateway_accepted:self.store.finish(control["event_id"],True)
                    except Exception:self.store.finish(control["event_id"],False)
                    finally:_current_event.reset(token)
            try:await asyncio.wait_for(self._completion.wait(),timeout=.2)
            except asyncio.TimeoutError:pass

    async def on_processing_complete(self,event,outcome):
        self.store.finish(event.metadata.get("squire_event_id",event.message_id),outcome==ProcessingOutcome.SUCCESS)
        if self._completion:self._completion.set()

    async def _outbox_loop(self):
        while self._running:
            await asyncio.to_thread(self.broker.flush,self.store)
            await asyncio.sleep(.5)

    async def _media_cleanup_loop(self):
        from .media import cleanup
        while self._running:
            await asyncio.to_thread(cleanup,self.scope)
            await asyncio.sleep(.5)

    async def _approvals_loop(self):
        while self._running:
            try:
                response=await asyncio.to_thread(self.broker.request,"GET","/v1/approvals")
                for approval in response.get("approvals",[]):
                    event={"event_id":"approval:"+approval["approval_id"],"conversation_id":self.scope.conversation_id,"tenant_id":self.scope.tenant_id,"runtime_id":self.scope.runtime_id,"chat_type":"dm","sender_id":"owner-approved-request","provider_message_id":approval["approval_id"],"timestamp":float(approval["expires_at"])-600,"payload":{"text":"The owner explicitly approved this group-requested private action. Perform only the approved action and report the result privately; do not share anything with the group. Approved request: "+approval["summary"],"expires_at":int(approval["expires_at"])}}
                    self.store.accept([event],self.scope)
            except Exception:pass  # Dependency unavailable: retain state, never invent approval.
            await asyncio.sleep(15)

    async def send(self,chat_id,content,reply_to=None,metadata=None,*,_media=None):
        if str(chat_id)!=self.scope.conversation_id:return SendResult(success=False,error="destination outside runtime scope",error_kind="forbidden")
        if not content and not _media:return SendResult(success=True,message_id="empty")
        event=_current_event.get();event_id=event.metadata.get("squire_event_id") if event else None
        deadline=event.metadata.get("squire_expires_at",0) if event else None
        if expired(deadline):return SendResult(success=False,error="source message expired",error_kind="expired")
        if event_id:
            ordinal=self._send_counts.get(event_id,0);self._send_counts[event_id]=ordinal+1
            key="event:"+event_id+":"+str(ordinal)
        else:
            from .scheduled import response_key
            identity=str(content)+(":"+hashlib.sha256(_media["data_base64"].encode()).hexdigest() if _media else "")
            key=response_key(identity)
            if not key:
                operation=os.environ.get("SQUIRE_OUTBOUND_OPERATION_ID")
                if metadata and metadata.get("job_id"):
                    return SendResult(success=False,error="scheduled delivery identity missing",error_kind="invalid")
                if operation:
                    key="operation:"+hashlib.sha256((operation+":"+str(content)).encode()).hexdigest()
                else:
                    import uuid
                    key="adhoc:"+str(uuid.uuid4())
        payload={"conversation_id":self.scope.conversation_id,"response_key":key,"text":str(content)[:32000],"priority":"interactive" if event_id else "scheduled"}
        if event:payload["_expires_at"]=deadline
        if _media:payload["media"]=_media
        if event_id and not event_id.startswith("approval:"):payload["cause_event_id"]=event_id
        if event and event.metadata.get("squire_approval_id") and _final_send.get() and not _media:payload["_approval_id"]=event.metadata["squire_approval_id"]
        if reply_to and event:payload["reply_to"]=event.metadata.get("squire_provider_message_id",reply_to)
        self.store.enqueue(payload)
        return SendResult(success=True,message_id=key)

    async def _send_file(self,chat_id,path,kind,caption=None,reply_to=None,metadata=None,file_name=None):
        import base64
        import mimetypes
        from pathlib import Path
        safe=self.validate_media_delivery_path(str(path))
        if not safe:return SendResult(success=False,error="attachment path rejected",error_kind="invalid")
        file=Path(safe)
        try:
            if not file.is_file() or file.stat().st_size>10*1024*1024:return SendResult(success=False,error="attachment exceeds pilot size limit",error_kind="invalid")
            with file.open("rb") as stream:body=stream.read(10*1024*1024+1)
            if len(body)>10*1024*1024:return SendResult(success=False,error="attachment exceeds pilot size limit",error_kind="invalid")
        except OSError:return SendResult(success=False,error="attachment unavailable",error_kind="invalid")
        mime=mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        if kind=="audio" and file.suffix.lower()==".ogg":mime="audio/ogg"
        name=Path(file_name or file.name).name.replace("\\","_")[:160] or "attachment"
        media={"data_base64":base64.b64encode(body).decode(),"mime_type":mime,"filename":name,"media_type":kind,"ptt":kind=="audio" and mime=="audio/ogg"}
        return await self.send(chat_id,caption or "",reply_to,metadata,_media=media)

    async def send_image_file(self,chat_id,image_path,caption=None,reply_to=None,metadata=None,**kwargs):
        return await self._send_file(chat_id,image_path,"image",caption,reply_to,metadata)

    async def send_voice(self,chat_id,audio_path,caption=None,reply_to=None,metadata=None,**kwargs):
        return await self._send_file(chat_id,audio_path,"audio",caption,reply_to,metadata)

    async def send_video(self,chat_id,video_path,caption=None,reply_to=None,metadata=None,**kwargs):
        return await self._send_file(chat_id,video_path,"video",caption,reply_to,metadata)

    async def send_document(self,chat_id,file_path,caption=None,file_name=None,reply_to=None,metadata=None,**kwargs):
        return await self._send_file(chat_id,file_path,"document",caption,reply_to,metadata,file_name)

    async def send_final_ledgered(self,*args,**kwargs):
        token=_final_send.set(True)
        try:return await super().send_final_ledgered(*args,**kwargs)
        finally:_final_send.reset(token)

    async def get_chat_info(self,chat_id):
        if str(chat_id)!=self.scope.conversation_id:raise ValueError("destination outside runtime scope")
        return {"name":"Squire conversation","type":self.scope.kind}

    def toolsets_for_source(self,source):
        # Nonempty, explicit replacement; no empty-list -> all-tools fallback.
        return ["memory","squire-group"] if self.scope.kind=="group" else None


async def standalone_send(config,chat_id,message,*,thread_id=None,media_files=None,force_document=False):
    adapter=SquireAdapter(config)
    result=await adapter.send(chat_id,message)
    if not result.success:return {"error":result.error}
    from pathlib import Path
    for path,is_voice in media_files or []:
        suffix=Path(path).suffix.lower()
        kind="document" if force_document else "audio" if is_voice or suffix in {".ogg",".mp3",".m4a",".wav"} else "image" if suffix in {".jpg",".jpeg",".png",".webp",".gif"} else "video" if suffix in {".mp4",".mov",".webm"} else "document"
        result=await adapter._send_file(chat_id,path,kind)
        if not result.success:return {"error":result.error}
    await asyncio.to_thread(adapter.broker.flush,adapter.store)
    return {"success":True,"message_id":result.message_id}
