import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from whatsapp_transport.config import Settings
from whatsapp_transport.main import create_app
from whatsapp_transport.media import FetchMedia, MAX_MEDIA_BYTES, Media
from whatsapp_transport.normalize import change_event
from whatsapp_transport.broker import Broker, Send
from whatsapp_transport.ledger import Ledger, Conflict
from whatsapp_transport.supervisor import Supervisor
from whatsapp_transport.cli import CLIError


@pytest.fixture
def settings(tmp_path):
    value = Settings(data_dir=tmp_path, provider_account_id="pilot", broker_token="b"*32,
        operator_token="o"*32, MESSAGING_TRANSPORT_TOKEN="m"*32, messaging_url="http://messaging",
        require_mount=False, sync_enabled=False)
    value.store_dir.mkdir()
    with sqlite3.connect(value.store_dir / "wacli.db") as db:
        db.execute("CREATE TABLE messages(chat_jid,msg_id,media_type,mime_type,filename,file_length,revoked,deleted_for_me,deleted_at,payload_purged_at)")
        db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)", ("123@s.whatsapp.net", "known", "audio", "audio/ogg; codecs=opus", "../../voice\r\n.ogg", 5, 0, 0, 0, 0))
    return value


class Download:
    def __init__(self):
        self.calls = []
        self.target = None

    async def run(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        self.target = Path(args[-1])
        self.target.write_bytes(b"voice")
        return {"path": str(self.target)}


async def test_media_auth_scope_readonly_and_cleanup(settings):
    cli = Download()
    app = create_app(settings, cli=cli)
    request = {"provider_account_id": "pilot", "chat_jid": "123@s.whatsapp.net", "provider_message_id": "known"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://transport") as client:
        assert (await client.post("/v1/media/fetch", json=request)).status_code == 401
        headers = {"Authorization": "Bearer " + settings.broker_token}
        for update in ({"provider_account_id": "other"}, {"chat_jid": "456@s.whatsapp.net"}, {"provider_message_id": "unknown"}):
            assert (await client.post("/v1/media/fetch", json=request | update, headers=headers)).status_code == 404
        response = await client.post("/v1/media/fetch", json=request, headers=headers)
        assert response.content == b"voice"
        assert response.headers["content-type"] == "audio/ogg"
        assert response.headers["x-media-type"] == "audio"
        assert not cli.target.parent.exists()
        assert len(cli.calls) == 1 and cli.calls[0][1] == {"readonly": True}
    await app.state.capture.client.aclose()


async def test_media_rejects_oversize_and_deleted_before_download(settings):
    cli = Download()
    media = Media(settings, cli)
    request = FetchMedia(provider_account_id="pilot", chat_jid="123@s.whatsapp.net", provider_message_id="known")
    with sqlite3.connect(settings.store_dir / "wacli.db") as db:
        db.execute("UPDATE messages SET file_length=?", (MAX_MEDIA_BYTES + 1,))
    with pytest.raises(HTTPException) as error:
        await media.fetch(request)
    assert error.value.status_code == 413
    with sqlite3.connect(settings.store_dir / "wacli.db") as db:
        db.execute("UPDATE messages SET revoked=1")
    with pytest.raises(HTTPException) as error:
        await media.fetch(request)
    assert error.value.status_code == 404 and not cli.calls


def test_descriptor_has_no_provider_secret():
    change = {"sequence": 1, "chat_jid": "123@s.whatsapp.net", "msg_id": "known", "operation": "upsert", "message_time": 100}
    message = {"MediaType": "audio", "MimeType": "audio/ogg", "Filename": "voice.ogg", "MediaKey": "secret", "DirectPath": "/secret"}
    event = change_event("store", change, message, 0, True)
    assert event["media"] == {"type": "audio", "mime_type": "audio/ogg", "filename": "voice.ogg"}
    assert "secret" not in str(event)
    assert "media" not in change_event("store", change, message | {"Revoked": True}, 0, True)


async def test_outgoing_media_private_temp_idempotency_and_uncertain(settings):
    import base64
    class Sender:
        def __init__(self):
            self.calls = 0
            self.error = False
        async def run(self, *args):
            self.calls += 1
            assert args[:2] == ("send", "file")
            self.target = Path(args[args.index("--file") + 1])
            assert self.target.read_bytes() == b"voice"
            assert self.target.stat().st_mode & 0o777 == 0o600
            assert "--ptt" in args and "--max-size-mb" in args
            if self.error:
                raise CLIError("cli_outcome_unknown")
            return {"sent": True, "id": "provider-id"}
    cli = Sender()
    ledger = Ledger(settings.data_dir / "ledger.db")
    ledger.initialize(1, True)
    ledger.put("capture_enabled", True)
    supervisor = Supervisor(settings, cli, ledger)
    supervisor.connected = True
    broker = Broker(settings, cli, ledger, supervisor, type("Capture", (), {"error": None})())
    request = Send(operation_id="media-operation", provider_account_id="pilot", generation=1,
        recipient="123@s.whatsapp.net", kind="media", media={"data_base64": base64.b64encode(b"voice").decode(),
            "mime_type": "audio/ogg", "filename": "voice.ogg", "media_type": "audio", "ptt": True})
    assert (await broker.send(request))["status"] == "accepted"
    assert not cli.target.exists()
    assert (await broker.send(request))["status"] == "accepted" and cli.calls == 1
    cli.error = True
    uncertain = request.model_copy(update={"operation_id": "media-uncertain"})
    assert (await broker.send(uncertain))["status"] == "uncertain"
    assert not cli.target.exists()
    assert (await broker.send(uncertain))["status"] == "uncertain" and cli.calls == 2
    changed = request.model_copy(update={"text": "changed"})
    with pytest.raises(Conflict):
        await broker.send(changed)


async def test_temporary_download_removed_even_if_client_disconnects(tmp_path, monkeypatch):
    from fastapi.responses import FileResponse
    from whatsapp_transport.responses import EphemeralFileResponse
    root = tmp_path / "private"
    root.mkdir()
    path = root / "payload"
    path.write_bytes(b"private")
    async def disconnect(*args, **kwargs):
        raise OSError("client disconnected")
    monkeypatch.setattr(FileResponse, "__call__", disconnect)
    response = EphemeralFileResponse(path, private_root=root)
    with pytest.raises(OSError):
        await response({}, None, None)
    assert not root.exists()
