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
    event=MessageEvent(text="Describe",message_id="event-a")
    attach(broker,scope,event,{"type":"image","filename":"../../private-key","path":"/etc/passwd","url":"https://external.invalid"})
    path=Path(event.media_urls[0])
    assert path.parent==scope.home/".squire/media" and path.suffix==".png"
    assert path.read_bytes()==b"image-bytes" and path.stat().st_mode&0o777==0o600
    assert event.message_type==MessageType.PHOTO and event.media_types==["image/png"]
    broker.fetch_media.assert_called_once_with("event-a")


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
