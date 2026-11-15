import httpx
from sqlmodel import Session, select
from messaging import media
from messaging.db import engine
from messaging.models import Binding, InboxEvent, Outbox
from conftest import headers
from test_messaging import imported, runtime, event, ingest


def attachment_event():
    binding = imported()
    runtime(binding)
    ingest([event(media={"type": "image", "mime_type": "image/png"})])
    with Session(engine()) as db:
        row = db.exec(select(InboxEvent)).first()
        return binding, row.id


def transport(monkeypatch, handler):
    monkeypatch.setattr(media, "broker_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))


def test_media_scoped_to_stored_event_and_no_provider_metadata(client, monkeypatch):
    binding, eid = attachment_event()
    other = imported("other", "15550000002@s.whatsapp.net")
    runtime(other, tenant="other", token="x" * 40)
    seen = []
    def response(req):
        import json
        seen.append(json.loads(req.content))
        assert req.headers["authorization"] == "Bearer " + "p" * 40
        return httpx.Response(200, content=b"image bytes", headers={"content-type": "image/png", "x-provider-url": "private"})
    transport(monkeypatch, response)
    assert client.get("/v1/media/" + eid, headers={"authorization": "Bearer " + "x" * 40}).status_code == 404
    assert not seen
    result = client.get("/v1/media/" + eid, headers=headers("dm"))
    assert result.status_code == 200 and result.content == b"image bytes"
    assert "x-provider-url" not in result.headers
    assert result.headers["cache-control"] == "no-store"
    assert seen == [{"provider_account_id": "squire", "chat_jid": "15550000001@s.whatsapp.net", "provider_message_id": "provider-1"}]


def test_media_stream_size_limit_without_content_length(client, monkeypatch):
    _, eid = attachment_event()
    monkeypatch.setattr(media, "MAX_BYTES", 8)
    transport(monkeypatch, lambda req: httpx.Response(200, stream=httpx.ByteStream(b"ninebytes")))
    assert client.get("/v1/media/" + eid, headers=headers("dm")).status_code == 413


def test_media_revocation_during_fetch_denies_buffered_bytes(client, monkeypatch):
    binding, eid = attachment_event()
    def revoke(req):
        with Session(engine()) as db:
            row = db.get(Binding, binding); row.active = False; db.add(row); db.commit()
        return httpx.Response(200, content=b"must not escape")
    transport(monkeypatch, revoke)
    assert client.get("/v1/media/" + eid, headers=headers("dm")).status_code == 404


def test_download_releases_database_connection_and_rejects_parallel_runtime(client, monkeypatch):
    from sqlalchemy import event as sql_event
    _, eid = attachment_event()
    checked_out = set()
    def checkout(conn, record, proxy): checked_out.add(id(record))
    def checkin(conn, record): checked_out.discard(id(record))
    sql_event.listen(engine(), "checkout", checkout)
    sql_event.listen(engine(), "checkin", checkin)
    def response(req):
        assert not checked_out, "network download must not hold a DB connection"
        concurrent = client.get("/v1/media/" + eid, headers=headers("dm"))
        assert concurrent.status_code == 429
        assert not checked_out
        return httpx.Response(200, content=b"bounded attachment")
    transport(monkeypatch, response)
    try:
        assert client.get("/v1/media/" + eid, headers=headers("dm")).status_code == 200
        assert not media._fetching
    finally:
        sql_event.remove(engine(), "checkout", checkout)
        sql_event.remove(engine(), "checkin", checkin)


def test_deleted_or_old_attachment_cannot_be_fetched(client, monkeypatch):
    _, eid = attachment_event()
    transport(monkeypatch, lambda req: (_ for _ in ()).throw(AssertionError("broker must not be called")))
    with Session(engine()) as db:
        row = db.get(InboxEvent, eid); row.created_at -= media.MAX_AGE_SECONDS + 1; db.add(row); db.commit()
    assert client.get("/v1/media/" + eid, headers=headers("dm")).status_code == 404
    with Session(engine()) as db:
        row = db.get(InboxEvent, eid); row.created_at = media.now(); row.payload_enc = None; db.add(row); db.commit()
    assert client.get("/v1/media/" + eid, headers=headers("dm")).status_code == 404


def test_outbound_attachment_is_encrypted_scoped_and_immutable(client):
    import base64
    from messaging.crypto import open_sealed
    binding = imported(); runtime(binding)
    other = imported("other", "15550000002@s.whatsapp.net")
    body = {"conversation_id": binding, "response_key": "media-1", "media": {
        "data_base64": base64.b64encode(b"private attachment sentinel").decode(),
        "mime_type": "application/pdf", "filename": "report.pdf", "media_type": "document"}}
    first = client.post("/v1/outbound", headers=headers("dm"), json=body)
    assert first.status_code == 200, first.text
    assert client.post("/v1/outbound", headers=headers("dm"), json=body).json() == first.json()
    with Session(engine()) as db:
        row = db.get(Outbox, first.json()["operation_id"])
        assert body["media"]["data_base64"] not in row.payload_enc
        assert open_sealed(row.payload_enc, "outbox:" + row.id)["media"] == dict(body["media"], ptt=False)
    body["conversation_id"] = other
    assert client.post("/v1/outbound", headers=headers("dm"), json=body).status_code == 403
    body["conversation_id"] = binding
    body["media"]["data_base64"] = base64.b64encode(b"changed file").decode()
    assert client.post("/v1/outbound", headers=headers("dm"), json=body).status_code == 409
    body["media"]["filename"] = "../private.txt"
    assert client.post("/v1/outbound", headers=headers("dm"), json=body).status_code == 422


def test_attachment_worker_forwards_bytes_once_with_same_durable_id(client):
    import base64
    import json
    from test_messaging import enable
    from messaging.worker import claim_outbound, send
    binding = imported(); runtime(binding); enable()
    body = {"conversation_id": binding, "response_key": "media-worker", "text": "caption", "media": {
        "data_base64": base64.b64encode(b"synthetic PDF bytes").decode(),
        "mime_type": "application/pdf", "filename": "report.pdf", "media_type": "document"}}
    result = client.post("/v1/outbound", headers=headers("dm"), json=body)
    assert result.status_code == 200
    claim = claim_outbound(); sent = []
    def handler(req):
        if req.url.path.endswith("entitlement"):
            return httpx.Response(200, json={"allowed": True, "state": "imported_active", "runtime_ready": True})
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"status": "accepted", "provider_message_id": "provider-media"})
    with httpx.Client(transport=httpx.MockTransport(handler)) as broker:
        send(claim, broker); send(claim, broker)
    assert len(sent) == 1
    assert sent[0]["operation_id"] == result.json()["operation_id"]
    assert sent[0]["kind"] == "media" and sent[0]["media"]["data_base64"] == body["media"]["data_base64"]
    assert sent[0]["recipient"] == "15550000001@s.whatsapp.net"
    with Session(engine()) as db:
        assert db.get(Outbox, claim.id).state == "accepted"
