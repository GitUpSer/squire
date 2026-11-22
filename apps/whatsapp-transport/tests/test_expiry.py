import asyncio
import os
import sqlite3
import time

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from test_transport import settings, broker, send, Remote
from test_media import settings as media_settings, Download
from whatsapp_transport.capture import Capture
from whatsapp_transport.cli import Wacli
from whatsapp_transport.ledger import Ledger
from whatsapp_transport.media import Media, FetchMedia
from whatsapp_transport.normalize import change_event, expiry_metadata
from whatsapp_transport.retention import Retention
from whatsapp_transport.supervisor import Supervisor


def test_capture_deadline_is_explicit_and_content_free_on_expiry(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 1000)
    change = dict(sequence=7, chat_jid="123@s.whatsapp.net", msg_id="ttl", operation="upsert", message_time=970)
    message = dict(Text="private", MediaType="image", Timestamp=970)
    for known, deadline, blocked in [(True, None, False), (True, 1030, False), (True, 1000, True), (False, None, True)]:
        event = change_event("store", change, message, 900, True, expiry_known=known, expires_at=deadline)
        assert event["sequence"] == 7
        if blocked:
            assert event["kind"] == "delete" and "text" not in event and "media" not in event
        else:
            assert event["kind"] == "message" and event["expires_at"] == deadline
    assert change_event("store", change, message, 0)["kind"] == "delete"
    moved = change_event("store", change, message | {"ChatJID": "456@s.whatsapp.net"}, 0,
                         expiry_known=True, expires_at=999)
    assert moved["chat_jid"] == "456@s.whatsapp.net" and moved["kind"] == "delete"


@pytest.mark.parametrize("deadline", [-1, "100", 1.5, 253402300800])
def test_malformed_provider_metadata_is_unknown(deadline):
    with sqlite3.connect(":memory:") as db:
        assert expiry_metadata(db, "chat", "id") == (False, None)
        db.execute("CREATE TABLE squire_message_expiry(chat_jid,msg_id,known,expires_at)")
        db.execute("INSERT INTO squire_message_expiry VALUES('chat','id',1,?)", (deadline,))
        assert expiry_metadata(db, "chat", "id") == (False, None)


@pytest.mark.parametrize("during", [False, True])
async def test_media_expiry_before_or_during_download(media_settings, monkeypatch, during):
    clock = [1000]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    with sqlite3.connect(media_settings.store_dir / "wacli.db") as db:
        db.execute("UPDATE squire_message_expiry SET expires_at=?", (1001 if during else 1000,))
    class DownloadAcrossDeadline(Download):
        async def run(self, *args, **kwargs):
            result = await super().run(*args, **kwargs)
            clock[0] = 1001
            return result
    cli = DownloadAcrossDeadline()
    request = FetchMedia(provider_account_id="pilot", chat_jid="123@s.whatsapp.net", provider_message_id="known")
    with pytest.raises(HTTPException) as error:
        await Media(media_settings, cli).fetch(request)
    assert error.value.status_code == 404
    assert len(cli.calls) == int(during)
    if during:
        assert not cli.target.parent.exists()


async def test_legacy_media_is_quarantined(media_settings):
    with sqlite3.connect(media_settings.store_dir / "wacli.db") as db:
        db.execute("UPDATE squire_message_expiry SET known=0")
    cli = Download()
    request = FetchMedia(provider_account_id="pilot", chat_jid="123@s.whatsapp.net", provider_message_id="known")
    with pytest.raises(HTTPException):
        await Media(media_settings, cli).fetch(request)
    assert not cli.calls


async def test_queued_send_expiry_and_absolute_cli_deadline(settings, monkeypatch):
    clock = [1000]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    instance = broker(settings)
    await instance.supervisor.lock.acquire()
    task = asyncio.create_task(instance.send(send(expires_at=1001)))
    await asyncio.sleep(0)
    clock[0] = 1001
    instance.supervisor.lock.release()
    assert await task == dict(operation_id="operation-1", status="definitely_unsent", error_code="message_expired", retryable=False)
    assert not instance.cli.calls
    assert (await instance.send(send(expires_at=1060)))["status"] == "accepted"
    assert instance.cli.calls[0][-2:] == ("--squire-expires-at", "1060")
    clock[0] = 1070
    assert (await instance.send(send(expires_at=1060)))["status"] == "accepted"
    assert len(instance.cli.calls) == 1  # already accepted remains accepted, never retried
    for value in [True, 1.5, "123", 0, 253402300800]:
        with pytest.raises(ValidationError):
            send(expires_at=value)


@pytest.mark.skipif(not os.environ.get("WACLI_TEST_BINARY"), reason="requires expiry-patched CLI")
async def test_actual_journal_short_ttl_and_legacy_purge_ack_recovery(settings, monkeypatch):
    settings.wacli_binary = os.environ["WACLI_TEST_BINARY"]
    cli = Wacli(settings)
    await cli.run("doctor")
    ledger = Ledger(settings.data_dir / "ledger.db")
    remote = Remote()
    capture = Capture(settings, cli, ledger, remote.client())
    path = settings.store_dir / "wacli.db"
    now = int(time.time())
    chat = "15555550123@s.whatsapp.net"
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO chats(jid,kind,name,last_message_ts) VALUES (?,'dm','Fixture',?)", (chat, now))
        for message in ("expired", "legacy", "ordinary"):
            db.execute("INSERT INTO messages(chat_jid,msg_id,sender_jid,ts,from_me,text) VALUES (?,?,?,?,0,'private')", (chat, message, chat, now))
            db.execute("INSERT INTO squire_message_expiry VALUES(?,?,?,?)", (chat, message, int(message != "legacy"), now - 1 if message == "expired" else None))
    # All rows predate baseline, demonstrating unknown history is not stranded.
    await capture.activate()
    store_id = ledger.get("store_id")
    retention = Retention(settings, cli, ledger, Supervisor(settings, cli, ledger), capture)
    # Read-only due probe prioritizes short TTL over bounded legacy cleanup.
    with sqlite3.connect(path) as db:
        assert retention.expired(db, 1) == [(chat, "expired")]
    remote.fail_ack = True
    with pytest.raises(httpx.ReadTimeout):
        await retention.run(dry_run=False)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM messages WHERE payload_purged_at IS NOT NULL").fetchone()[0] == 0
    remote.fail_ack = False
    result = await retention.run(dry_run=False)
    assert result["purged"] == 2
    assert ledger.get("store_id") == store_id
    assert await capture.once() is False  # retained journal floor still permits recovery
    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT msg_id,text,payload_purged_at FROM messages ORDER BY msg_id").fetchall()
        assert rows[0][1] is None and rows[0][2]
        assert rows[1][1] is None and rows[1][2]
        assert rows[2] == ("ordinary", "private", None)
    assert remote.events and all(e["kind"] == "delete" and "text" not in e and "media" not in e for e in remote.events)
    await capture.client.aclose()


async def test_idle_expiry_sweep_does_not_interrupt_sync(media_settings, monkeypatch):
    ledger = Ledger(media_settings.data_dir / "ledger.db")
    ledger.put("capture_enabled", True)
    retention = Retention(media_settings, None, ledger, None, None)
    sleeps = []
    async def tick(delay):
        sleeps.append(delay)
        if len(sleeps) == 2:
            raise asyncio.CancelledError
    async def unexpected(**kwargs):
        pytest.fail("idle expiry sweep acquired sync ownership")
    monkeypatch.setattr(asyncio, "sleep", tick)
    monkeypatch.setattr(retention, "run", unexpected)
    with pytest.raises(asyncio.CancelledError):
        await retention.follow()
    assert sleeps == [5, 5]
