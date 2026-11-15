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
from .store import Store
from .client import Broker

_current_event=contextvars.ContextVar("squire_event",default=None)


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
        self._model_ready=await asyncio.to_thread(model_configured,self.scope)
        for event_id in self.store.recover():
            self.store.enqueue({"conversation_id":self.scope.conversation_id,"response_key":"recovery:"+event_id,"text":"My last task was interrupted and may have taken an action. I have paused queued work so I don’t repeat it. Please ask the pilot operator to reconcile this task before continuing.","priority":"interactive","cause_event_id":event_id})
        self.store.mark_runtime(self.scope.generation,True,self._model_ready)
        self._tasks=[asyncio.create_task(self._inbox_loop()),asyncio.create_task(self._outbox_loop())]
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
            payload=envelope["payload"]
            source=self.build_source(chat_id=self.scope.conversation_id,chat_name="Private Squire" if self.scope.kind=="dm" else "Approved group",chat_type=self.scope.kind,user_id=envelope["sender_id"],message_id=envelope["provider_message_id"])
            text=payload.get("text","")
            if self.scope.kind=="group" and text.lstrip().lower().startswith("!squire "):text=text.lstrip()[8:]
            event=MessageEvent(text=text,source=source,user_id=envelope["sender_id"],message_id=envelope["event_id"],reply_to_message_id=payload.get("reply_to"),timestamp=datetime.fromtimestamp(envelope["timestamp"],timezone.utc),metadata={"squire_event_id":envelope["event_id"],"squire_provider_message_id":envelope["provider_message_id"]},allow_gateway_control=self.scope.kind=="dm")
            if envelope["event_id"].startswith("approval:"):event.metadata["squire_approval_id"]=envelope["event_id"][9:]
            token=_current_event.set(event);self._completion=asyncio.Event()
            try:
                if payload.get("media"):
                    # Opaque media capabilities are never treated as a caller-supplied URL/path.
                    # Text-only pilot rejects unsupported content visibly, without model/tool work.
                    await self.send(source.chat_id,"This pilot currently accepts text messages. Please send the relevant text from that attachment.")
                    self.store.finish(envelope["event_id"],True);continue
                await self.handle_message(event)
                if not event._gateway_accepted:
                    self.store.finish(envelope["event_id"],False)
                else:
                    await self._completion.wait()
            except asyncio.CancelledError:raise
            except Exception:
                self.store.finish(envelope["event_id"],False)
            finally:
                _current_event.reset(token);self._completion=None

    async def on_processing_complete(self,event,outcome):
        self.store.finish(event.metadata.get("squire_event_id",event.message_id),outcome==ProcessingOutcome.SUCCESS)
        if self._completion:self._completion.set()

    async def _outbox_loop(self):
        while self._running:
            await asyncio.to_thread(self.broker.flush,self.store)
            await asyncio.sleep(.5)

    async def _approvals_loop(self):
        while self._running:
            try:
                response=await asyncio.to_thread(self.broker.request,"GET","/v1/approvals")
                for approval in response.get("approvals",[]):
                    event={"event_id":"approval:"+approval["approval_id"],"conversation_id":self.scope.conversation_id,"tenant_id":self.scope.tenant_id,"runtime_id":self.scope.runtime_id,"chat_type":"dm","sender_id":"owner-approved-request","provider_message_id":approval["approval_id"],"timestamp":float(approval["expires_at"])-600,"payload":{"text":"The owner explicitly approved this group-requested private action. Perform only the approved action and report the result privately; do not share anything with the group. Approved request: "+approval["summary"]}}
                    self.store.accept([event],self.scope)
            except Exception:pass  # Dependency unavailable: retain state, never invent approval.
            await asyncio.sleep(15)

    async def send(self,chat_id,content,reply_to=None,metadata=None):
        if str(chat_id)!=self.scope.conversation_id:return SendResult(success=False,error="destination outside runtime scope",error_kind="forbidden")
        if not content:return SendResult(success=True,message_id="empty")
        event=_current_event.get();event_id=event.metadata.get("squire_event_id") if event else None
        if event_id:
            ordinal=self._send_counts.get(event_id,0);self._send_counts[event_id]=ordinal+1
            key="event:"+event_id+":"+str(ordinal)
        else:
            # Standalone cron sends are durably queued once by the caller. Provider retries
            # use this response key; callers must not rerun unknown scheduled side effects.
            import uuid
            key="scheduled:"+str(uuid.uuid4())
        payload={"conversation_id":self.scope.conversation_id,"response_key":key,"text":str(content)[:32000],"priority":"interactive" if event_id else "scheduled"}
        if event_id and not event_id.startswith("approval:"):payload["cause_event_id"]=event_id
        if event and event.metadata.get("squire_approval_id"):payload["_approval_id"]=event.metadata["squire_approval_id"]
        if reply_to and event:payload["reply_to"]=event.metadata.get("squire_provider_message_id",reply_to)
        self.store.enqueue(payload)
        return SendResult(success=True,message_id=key)

    async def get_chat_info(self,chat_id):
        if str(chat_id)!=self.scope.conversation_id:raise ValueError("destination outside runtime scope")
        return {"name":"Squire conversation","type":self.scope.kind}

    def toolsets_for_source(self,source):
        # Nonempty, explicit replacement; no empty-list -> all-tools fallback.
        return ["memory","squire-group"] if self.scope.kind=="group" else None


async def standalone_send(config,chat_id,message,*,thread_id=None,media_files=None,force_document=False):
    if media_files:return {"error":"Squire pilot supports text output only"}
    adapter=SquireAdapter(config)
    result=await adapter.send(chat_id,message)
    if result.success:await asyncio.to_thread(adapter.broker.flush,adapter.store)
    return {"success":result.success,"message_id":result.message_id} if result.success else {"error":result.error}
