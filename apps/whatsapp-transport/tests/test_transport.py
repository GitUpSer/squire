import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path

import httpx
import pytest

from whatsapp_transport.broker import Broker, Send
from whatsapp_transport.capture import Capture, digest
from whatsapp_transport.cli import CLIError, Wacli
from whatsapp_transport.config import Settings
from whatsapp_transport.ledger import Conflict, Ledger
from whatsapp_transport.main import create_app
from whatsapp_transport.normalize import change_event, group_info
from whatsapp_transport.retention import Retention
from whatsapp_transport.supervisor import Supervisor


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, provider_account_id="pilot", broker_token="b" * 32,
                    operator_token="o" * 32, MESSAGING_TRANSPORT_TOKEN="m" * 32,
                    messaging_url="http://messaging", require_mount=False, sync_enabled=False)


class FakeCLI:
    def __init__(self):
        self.calls = []
        self.error = None

    async def run(self, *args, **kwargs):
        self.calls.append(args)
        if self.error:
            raise self.error
        return {"sent": True, "id": "provider-1"}


def broker(settings):
    ledger = Ledger(settings.data_dir / "ledger.db")
    ledger.initialize(1, True)
    ledger.put("capture_enabled", True)
    cli = FakeCLI()
    supervisor = Supervisor(settings, cli, ledger)
    supervisor.connected = True
    capture = type("CaptureState", (), {"error": None})()
    return Broker(settings, cli, ledger, supervisor, capture)


def send(**kw):
    return Send(operation_id="operation-1", provider_account_id="pilot", generation=1,
                recipient="+15555550123", kind="text", text=kw.pop("text", "hello"), **kw)


async def test_send_concurrent_idempotency_and_collision(settings):
    instance = broker(settings)
    results = await asyncio.gather(instance.send(send()), instance.send(send()))
    assert [r["status"] for r in results] == ["accepted", "accepted"]
    assert len(instance.cli.calls) == 1
    assert instance.cli.calls[0][3] == "15555550123@s.whatsapp.net"
    with pytest.raises(Conflict, match="payload_changed"):
        await instance.send(send(text="different"))


async def test_uncertain_never_retries_and_restart_marks_dispatch(settings):
    instance = broker(settings)
    instance.cli.error = CLIError("cli_outcome_unknown")
    assert (await instance.send(send()))["status"] == "uncertain"
    instance.cli.error = None
    assert (await instance.send(send()))["status"] == "uncertain"
    assert len(instance.cli.calls) == 1
    instance.ledger.begin_send("crashed-send", "fingerprint", 1)
    instance.ledger.initialize(1, True)
    with instance.ledger.connection() as db:
        assert db.execute("SELECT state FROM sends WHERE operation_id='crashed-send'").fetchone()[0] == "uncertain"


async def test_only_proven_unsent_can_retry(settings):
    instance = broker(settings)
    instance.cli.error = CLIError("queue_deadline", definitely_unsent=True)
    assert (await instance.send(send()))["retryable"]
    instance.cli.error = None
    assert (await instance.send(send()))["status"] == "accepted"
    assert len(instance.cli.calls) == 2


async def test_fence_disable_and_logout_prevent_dispatch(settings):
    instance = broker(settings)
    instance.ledger.control(generation=2, outbound_enabled=True)
    with pytest.raises(Conflict, match="stale_generation"):
        await instance.send(send())
    instance.ledger.control(generation=2, outbound_enabled=False)
    request = send().model_copy(update={"generation": 2})
    assert (await instance.send(request))["error_code"] == "outbound_paused"
    instance.ledger.control(generation=2, outbound_enabled=True)
    instance.ledger.put("logged_out", True)
    assert (await instance.send(request))["error_code"] == "not_connected"
    assert not instance.cli.calls


def test_only_verified_live_provenance_can_execute():
    change = dict(sequence=1, chat_jid="123@s.whatsapp.net", msg_id="msg", operation="upsert", message_time=100)
    message = dict(Text="hello", Timestamp=100, SenderJID="123@s.whatsapp.net")
    assert change_event("store", change, message, 50)["origin"] == "history"
    assert change_event("store", change, message, 50, verified_live=True)["origin"] == "replay"
    assert change_event("store", change, message, 150, verified_live=True)["origin"] == "history"


def test_verified_group_mapping_preserves_lid():
    group = group_info({"JID": "123@g.us", "Participants": [{"JID": "456@lid", "PhoneNumber": "123@s.whatsapp.net", "IsAdmin": True}]}, 99)
    assert group["participants"] == [{"jid": "456@lid", "phone_jid": "123@s.whatsapp.net", "admin": True}]


async def test_private_auth_validation_and_flock(settings):
    app = create_app(settings, cli=FakeCLI())
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://broker") as client:
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.get("/readyz")).status_code == 503
        assert (await client.get("/v1/status")).status_code == 401
        secret = "do-not-echo-this-message"
        response = await client.post("/v1/send", json={"text": secret}, headers={"Authorization": "Bearer " + settings.broker_token})
        assert response.status_code == 422 and secret not in response.text
        with pytest.raises(BlockingIOError):
            Supervisor(settings, FakeCLI(), app.state.ledger).acquire()


async def test_pairing_mode_no_writer_or_lock_and_mutations_disabled(settings):
    settings.pairing_mode = True
    cli = FakeCLI()
    app = create_app(settings, cli=cli)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://broker") as client:
        assert (await client.get("/healthz")).status_code == 200
        assert app.state.supervisor.task is None
        assert app.state.supervisor.owner is None
        other = Supervisor(settings, cli, app.state.ledger)
        other.acquire()
        other.owner.close()
        operator = {"Authorization": "Bearer " + settings.operator_token}
        assert (await client.get("/v1/status", headers=operator)).json()["pairing_mode"]
        assert (await client.post("/v1/operator/activate", headers=operator)).status_code == 503
        response = await client.post("/v1/send", json=send().model_dump(), headers={"Authorization": "Bearer " + settings.broker_token})
        assert response.status_code == 503
        assert not cli.calls


class Remote:
    def __init__(self):
        self.checkpoints, self.events = {}, []
        self.fail_ack = False

    def respond(self, request):
        if request.method == "GET":
            key = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={"checkpoint": self.checkpoints.get(key)})
        batch = json.loads(request.content)
        key = batch["store_id"]
        if self.checkpoints.get(key) != batch["checkpoint_before"]:
            return httpx.Response(409)
        self.checkpoints[key] = batch["checkpoint_after"]
        self.events.extend(batch["events"])
        if self.fail_ack:
            raise httpx.ReadTimeout("simulated lost acknowledgement")
        return httpx.Response(200, json={"checkpoint_after": batch["checkpoint_after"]})

    def client(self):
        return httpx.AsyncClient(base_url="http://messaging", transport=httpx.MockTransport(self.respond))


async def test_activation_recovers_remote_commit_lost_ack(settings):
    class CLI:
        async def run(self, *args, **kwargs):
            return dict(store_id="store", next_checkpoint="zero", changes=[])
    remote = Remote()
    remote.fail_ack = True
    ledger = Ledger(settings.data_dir / "ledger.db")
    capture = Capture(settings, CLI(), ledger, remote.client())
    with pytest.raises(httpx.ReadTimeout):
        await capture.activate()
    assert not ledger.get("capture_enabled")
    remote.fail_ack = False
    await capture.activate()
    assert ledger.get("capture_enabled") and ledger.get("store_id") == "store"


async def test_capture_lost_ack_recovers_marker_without_replaying(settings):
    change = dict(sequence=1, operation="upsert", chat_jid="123@s.whatsapp.net", msg_id="msg", message_time=100)
    class CLI:
        async def run(self, *args, **kwargs):
            if args[1] == "show":
                return dict(Text="hello", Timestamp=100)
            changes = [change] if args[3] == "zero" else []
            return dict(store_id="store", next_checkpoint="one", changes=changes, has_more=False)
    ledger = Ledger(settings.data_dir / "ledger.db")
    ledger.put("store_id", "store")
    ledger.put("capture_enabled", True)
    ledger.put("activated_at", 50)
    remote = Remote()
    remote.checkpoints["store"] = "zero"
    capture = Capture(settings, CLI(), ledger, remote.client())
    remote.fail_ack = True
    with pytest.raises(httpx.ReadTimeout):
        await capture.once()
    assert not ledger.was_captured(digest(change["chat_jid"]), digest("msg"))
    remote.fail_ack = False
    await capture.once()
    assert ledger.was_captured(digest(change["chat_jid"]), digest("msg"))
    assert len(remote.events) == 1


async def test_group_metadata_waits_for_send_and_stops_sync(settings):
    instance = broker(settings)
    stopped = []
    async def stop():
        stopped.append(True)
        instance.supervisor.connected = False
    instance.supervisor.stop_process = stop
    class GroupCLI:
        async def run(self, *args, **kwargs):
            assert stopped and instance.supervisor.paused
            assert args == ("groups", "info", "--jid", "123@g.us")
            return {"JID": "123@g.us", "Participants": [{"JID": "123@s.whatsapp.net", "IsAdmin": True}]}
    instance.cli = GroupCLI()
    await instance.supervisor.lock.acquire()
    task = asyncio.create_task(instance.group("123@g.us"))
    await asyncio.sleep(0)
    assert not stopped
    instance.supervisor.lock.release()
    snapshot = await task
    assert snapshot["participants"][0]["admin"]
    assert not instance.supervisor.paused


@pytest.mark.skipif(not os.environ.get("WACLI_TEST_BINARY"), reason="set WACLI_TEST_BINARY to pinned executable")
async def test_real_binary_journal_capture_receipts_and_safe_retention(settings):
    settings.wacli_binary = os.environ["WACLI_TEST_BINARY"]
    cli = Wacli(settings)
    doctor = await cli.run("doctor")
    assert doctor["authenticated"] is False
    ledger = Ledger(settings.data_dir / "ledger.db")
    remote = Remote()
    capture = Capture(settings, cli, ledger, remote.client())
    await capture.activate()
    path = settings.store_dir / "wacli.db"
    chat, msg = "15555550123@s.whatsapp.net", "offline-message"
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO chats(jid,kind,name,last_message_ts) VALUES (?,'dm','Fixture',?)", (chat, int(time.time())))
        db.execute("INSERT INTO messages(chat_jid,msg_id,sender_jid,ts,from_me,text) VALUES (?,?,?,?,0,?)",
                   (chat, msg, chat, int(time.time()) - 9 * 86400, "offline captured payload"))
    assert await capture.once() is False
    assert remote.events[-1]["text"] == "offline captured payload"
    assert remote.events[-1]["origin"] == "history"  # unproven direct mutation is quarantined
    assert ledger.was_captured(digest(chat), digest(msg))
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO message_receipts VALUES (?,?,?,?,?)", (chat, msg, chat, "read", int(time.time()) + 1))
    await capture.receipts()
    assert remote.events[-1]["receipt_status"] == "read"
    await capture.receipts()
    assert len([e for e in remote.events if e["kind"] == "receipt"]) == 1
    supervisor = Supervisor(settings, cli, ledger)
    retention = Retention(settings, cli, ledger, supervisor, capture)
    result = await retention.run(dry_run=False)
    assert result["purged"] == 1
    assert await capture.once() is False  # purge did not invalidate committed checkpoint
    with sqlite3.connect(path) as db:
        row = db.execute("SELECT text,payload_purged_at FROM messages WHERE msg_id=?", (msg,)).fetchone()
        assert row[0] is None and row[1] is not None


async def test_retention_pages_past_uncaptured_baseline(settings):
    ledger = Ledger(settings.data_dir / "ledger.db")
    ledger.put("capture_enabled", True)
    settings.store_dir.mkdir()
    with sqlite3.connect(settings.store_dir / "wacli.db") as db:
        db.execute("CREATE TABLE messages(chat_jid TEXT,msg_id TEXT,ts INTEGER,payload_purged_at INTEGER,deleted_at INTEGER,deletion_reason TEXT)")
        db.executemany("INSERT INTO messages(chat_jid,msg_id,ts,payload_purged_at) VALUES ('123@s.whatsapp.net',?,?,NULL)",
                       [(str(i), i) for i in range(1200)])
    ledger.capture_batch("checkpoint", [(digest("123@s.whatsapp.net"), digest("1199"), time.time())])
    class Capture:
        async def once(self): return False
    class CLI:
        async def run(self, *args, **kwargs): return {}
    supervisor = Supervisor(settings, CLI(), ledger)
    retention = Retention(settings, CLI(), ledger, supervisor, Capture())
    # First bounded scan advances over1000 uncaptured baseline entries.
    first = await retention.run(dry_run=False)
    assert first["scanned"] == 1000 and first["purged"] == 0
    second = await retention.run(dry_run=True)
    assert second["eligible"] == 1 and second["skipped_uncaptured"] == 199
