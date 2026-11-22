import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from squire_overlay.client import Broker,BrokerError

class Response(io.BytesIO):
    def __init__(self,body,headers):super().__init__(body);self.headers=headers


def test_scoped_fetch_rejects_overflow_without_trusting_length(scope,monkeypatch):
    response=Response(b"x"*(10*1024*1024+1),{"Content-Type":"image/png"})
    opener=Mock();opener.open.return_value=response
    monkeypatch.setattr("urllib.request.build_opener",lambda *args:opener)
    with pytest.raises(BrokerError) as error:Broker(scope).fetch_media("event/one")
    assert error.value.status==413
    request=opener.open.call_args.args[0]
    assert request.full_url==scope.messaging_url+"/v1/media/event%2Fone"
    assert request.get_header("Authorization")=="Bearer "+scope.egress_token


def test_attachment_uses_safe_local_path_and_actual_hermes_fields(scope):
    from gateway.platforms.event import MessageEvent,MessageType
    from squire_overlay.media import attach
    broker=Mock();broker.fetch_media.return_value=(b"image-bytes","image/png")
    event=MessageEvent(text="Describe",message_id="event-a",metadata={"squire_expires_at":None})
    attach(broker,scope,event,{"type":"image","filename":"../../private-key","path":"/etc/passwd","url":"https://external.invalid"})
    path=Path(event.media_urls[0])
    assert path.parent==scope.home/".squire/media" and path.suffix==".png"
    assert path.read_bytes()==b"image-bytes" and path.stat().st_mode&0o777==0o600
    assert event.message_type==MessageType.PHOTO and event.media_types==["image/png"]
    broker.fetch_media.assert_called_once_with("event-a")


def test_expired_download_never_reaches_cache(scope,monkeypatch):
    from gateway.platforms.event import MessageEvent
    from squire_overlay.media import attach
    clock=[1000];monkeypatch.setattr("squire_overlay.media.time.time",lambda:clock[0])
    event=MessageEvent(text="Describe",message_id="expiring",metadata={"squire_expires_at":1001})
    def fetch(_):clock[0]=1001;return b"expired bytes","image/png"
    broker=Mock();broker.fetch_media.side_effect=fetch
    with pytest.raises(BrokerError) as error:attach(broker,scope,event,{"type":"image"})
    assert error.value.status==410 and not list(scope.home.rglob("*.png"))
    broker.reset_mock()
    with pytest.raises(BrokerError):attach(broker,scope,event,{"type":"image"})
    broker.fetch_media.assert_not_called()


def test_cleanup_purges_unknown_legacy_media_without_racing_active_download(scope,monkeypatch):
    from squire_overlay.media import cleanup
    directory=scope.home/".squire/media";directory.mkdir(parents=True)
    legacy=directory/("a"*64+".png");legacy.write_bytes(b"unknown expiry")
    active=directory/".download-10001-active";active.write_bytes(b"writing")
    stale=directory/".download-10000-stale";stale.write_bytes(b"abandoned")
    unknown=directory/".download-legacy";unknown.write_bytes(b"unknown expiry")
    monkeypatch.setattr("squire_overlay.media.time.time",lambda:10000)
    cleanup(scope)
    assert not legacy.exists() and not stale.exists() and not unknown.exists() and active.exists()


def test_interrupted_attachment_temp_has_same_expiry_for_restart_cleanup(scope,monkeypatch):
    import os
    from gateway.platforms.event import MessageEvent
    from squire_overlay.media import attach,cleanup
    clock=[1000];monkeypatch.setattr("squire_overlay.media.time.time",lambda:clock[0])
    event=MessageEvent(text="",message_id="interrupted",metadata={"squire_expires_at":1001})
    broker=Mock();broker.fetch_media.return_value=(b"private voice","audio/ogg")
    # Leave the exact pre-rename bytes as a killed process would.
    orphan=[]
    def preserve_temp(source,destination):orphan.append(Path(source))
    monkeypatch.setattr("squire_overlay.media.os.replace",preserve_temp)
    original_unlink=os.unlink
    monkeypatch.setattr("squire_overlay.media.os.unlink",lambda path,*a,**kw:None if Path(path) in orphan else original_unlink(path,*a,**kw))
    attach(broker,scope,event,{"type":"audio"})
    assert len(orphan)==1 and orphan[0].name.startswith(".download-1001-") and orphan[0].exists()
    monkeypatch.setattr("squire_overlay.media.os.unlink",original_unlink)
    clock[0]=1001;cleanup(scope)
    assert not orphan[0].exists()


@pytest.mark.asyncio
async def test_idle_cache_cleanup_preserves_deadline_across_adapter_restart(hermes,scope,monkeypatch):
    import asyncio
    from gateway.config import PlatformConfig
    from gateway.platforms.event import MessageEvent
    from squire_overlay.adapter import SquireAdapter
    from squire_overlay.media import attach
    clock=[1000];monkeypatch.setattr("squire_overlay.media.time.time",lambda:clock[0])
    broker=Mock();broker.fetch_media.return_value=(b"short-lived","audio/ogg")
    short=MessageEvent(text="",message_id="short",metadata={"squire_expires_at":1001})
    ordinary=MessageEvent(text="",message_id="ordinary",metadata={"squire_expires_at":None})
    attach(broker,scope,short,{"type":"audio"});attach(broker,scope,ordinary,{"type":"audio"})
    short_path=Path(short.media_urls[0]);ordinary_path=Path(ordinary.media_urls[0])
    assert short_path.exists() and ordinary_path.exists()
    clock[0]=1001
    restarted=SquireAdapter(PlatformConfig());restarted._running=True
    monkeypatch.setattr(restarted.broker,"flush",lambda store:False)
    task=asyncio.create_task(restarted._media_cleanup_loop())
    try:
        for _ in range(40):
            if not short_path.exists():break
            await asyncio.sleep(.025)
        assert not short_path.exists() and ordinary_path.exists()
    finally:
        restarted._running=False;task.cancel()
        await asyncio.gather(task,return_exceptions=True)


@pytest.mark.asyncio
async def test_outbound_attachment_persisted_and_replayed_without_duplicate(hermes,scope,monkeypatch):
    import base64,json
    from gateway.config import PlatformConfig
    from squire_overlay.adapter import SquireAdapter
    from squire_overlay.scheduled import delivery_scope
    monkeypatch.setattr("cron.executions.get_execution",lambda _: {"job_id":"chart", "scheduled_instant":"same-daily-report"})
    path=scope.home/"chart.png";path.write_bytes(b"image-data")
    adapter=SquireAdapter(PlatformConfig())
    with delivery_scope({"id":"chart","execution_id":"attempt"}):first=await adapter.send_image_file(scope.conversation_id,str(path),"Daily chart")
    with delivery_scope({"id":"chart","execution_id":"attempt"}):second=await adapter.send_image_file(scope.conversation_id,str(path),"Daily chart")
    assert first.success and first.message_id==second.message_id
    with adapter.store.db() as db:
        rows=db.execute("SELECT payload FROM outbox").fetchall()
    assert len(rows)==1
    payload=json.loads(rows[0][0]);assert payload["conversation_id"]==scope.conversation_id
    assert base64.b64decode(payload["media"]["data_base64"])==b"image-data"
    assert payload["media"]["filename"]=="chart.png" and payload["media"]["media_type"]=="image"
    denied=await adapter.send_document("different-conversation",str(path))
    assert not denied.success
