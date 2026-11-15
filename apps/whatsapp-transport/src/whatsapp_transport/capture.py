import asyncio
import hashlib
import json
import sqlite3
import time

import httpx

from .cli import CLIError
from .normalize import change_event, chat_type


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Capture:
    def __init__(self, settings, cli, ledger, client=None):
        self.settings, self.cli, self.ledger = settings, cli, ledger
        self.client = client or httpx.AsyncClient(base_url=settings.messaging_url.rstrip("/"), timeout=30,
            headers={"Authorization": "Bearer " + settings.messaging_token})
        self.lock = asyncio.Lock()
        self.error = None
        self.last_commit = 0

    async def remote_checkpoint(self, store_id):
        response = await self.client.get(f"/v1/ingest/checkpoints/{store_id}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json().get("checkpoint")

    async def commit(self, store_id, before, after, events):
        response = await self.client.post("/v1/ingest/batches", json={
            "provider_account_id": self.settings.provider_account_id, "store_id": store_id,
            "checkpoint_before": before, "checkpoint_after": after, "events": events})
        response.raise_for_status()
        if response.json().get("checkpoint_after", response.json().get("checkpoint")) != after:
            raise RuntimeError("checkpoint_ack_mismatch")
        self.last_commit = time.time()

    async def activate(self):
        """Explicit baseline: never turn existing account history into new work."""
        async with self.lock:
            page = await self.cli.run("messages", "changes", "--checkpoint", readonly=True)
            store_id = page["store_id"]
            before = await self.remote_checkpoint(store_id)
            if before is not None:
                pending = self.ledger.get("activation_pending")
                if not pending or pending["store_id"] != store_id:
                    raise RuntimeError("store_already_activated")
                self.ledger.put("store_id", store_id)
                self.ledger.put("checkpoint", before)
                self.ledger.put("capture_enabled", True)
                self.ledger.put("activation_pending", None)
                return
            activated_at = self.ledger.get("activated_at", time.time()) if self.ledger.get("activation_pending") else time.time()
            # Persist activation before the remote transaction; retries cannot lower it.
            self.ledger.put("activated_at", activated_at)
            self.ledger.put("activation_pending", {"store_id": store_id})
            await self.commit(store_id, None, page["next_checkpoint"], [])
            self.ledger.put("store_id", store_id)
            self.ledger.put("checkpoint", page["next_checkpoint"])
            self.ledger.put("capture_enabled", True)
            self.ledger.put("activation_pending", None)

    def verified_live(self, sequence):
        path = self.settings.store_dir / "wacli.db"
        if not path.exists():
            return False
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='squire_change_origins'").fetchone()
            if not exists:
                return False
            row = db.execute("""SELECT origin.live FROM squire_change_origins origin
                JOIN message_changes change ON change.sequence=origin.sequence
                WHERE origin.sequence=? AND origin.sequence=(SELECT max(newest.sequence)
                    FROM message_changes newest WHERE newest.chat_jid=change.chat_jid AND newest.msg_id=change.msg_id)""",
                (sequence,)).fetchone()
            return bool(row and row[0] == 1)

    async def once(self):
        async with self.lock:
            store_id = self.ledger.get("store_id")
            if not store_id or not self.ledger.get("capture_enabled", False):
                return False
            # Remote commit is authoritative, including after an acknowledgement was lost.
            before = await self.remote_checkpoint(store_id)
            if before is None:
                raise RuntimeError("committed_checkpoint_missing")
            pending = self.ledger.get("capture_pending")
            if pending and pending["checkpoint"] == before:
                self.ledger.capture_batch(before, pending["records"])
                self.ledger.put("capture_pending", None)
            page = await self.cli.run("messages", "changes", "--since", before, "--limit", "100", readonly=True)
            if page["store_id"] != store_id:
                raise RuntimeError("store_identity_changed")
            events, records = [], []
            for change in page["changes"]:
                message = None
                if change["operation"] == "upsert":
                    try:
                        message = await self.cli.run("messages", "show", "--chat", change["chat_jid"],
                                                     "--id", change["msg_id"], readonly=True)
                    except CLIError:
                        # A later local deletion can remove a payload referenced by an old
                        # journal upsert. Confirm absence; transient CLI errors must still fail.
                        path = self.settings.store_dir / "wacli.db"
                        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
                            exists = db.execute("SELECT 1 FROM messages WHERE chat_jid=? AND msg_id=?",
                                (change["chat_jid"], change["msg_id"])).fetchone()
                        if exists:
                            raise
                events.append(change_event(store_id, change, message, self.ledger.get("activated_at", time.time()),
                                           verified_live=self.verified_live(change["sequence"])))
                records.append((digest(change["chat_jid"]), digest(change["msg_id"]), time.time()))
            if events or page["next_checkpoint"] != before:
                self.ledger.put("capture_pending", {"checkpoint": page["next_checkpoint"], "records": records})
                await self.commit(store_id, before, page["next_checkpoint"], events)
                self.ledger.capture_batch(page["next_checkpoint"], records)
                self.ledger.put("capture_pending", None)
            self.error = None
            return page["has_more"]

    async def receipts(self):
        async with self.lock:
            return await self._receipts()

    async def _receipts(self):
        """Receipts are not in upstream changes; persist a separate deduplicated journal."""
        store_id = self.ledger.get("store_id")
        path = self.settings.store_dir / "wacli.db"
        if not store_id or not path.exists():
            return
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            rows = db.execute("SELECT chat_jid,msg_id,recipient_jid,status,ts FROM message_receipts WHERE ts>=?",
                              (int(self.ledger.get("activated_at", time.time())),)).fetchall()
        with self.ledger.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS receipt_journal(sequence INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT UNIQUE,payload TEXT NOT NULL)")
            for chat, msg, recipient, status, timestamp in rows:
                if status not in {"delivered", "read", "played"}:
                    continue
                event_id = digest(json.dumps([store_id, chat, msg, recipient, status, timestamp]))
                event = dict(event_id=event_id, provider_message_id=msg, chat_jid=chat, sender_jid="",
                             kind="receipt", chat_type=chat_type(chat), origin="replay", timestamp=timestamp,
                             from_me=True, receipt_status=status, recipient_jid=recipient)
                db.execute("INSERT OR IGNORE INTO receipt_journal(event_id,payload) VALUES (?,?)", (event_id, json.dumps(event)))
        receipt_store = store_id + ":receipts"
        before = await self.remote_checkpoint(receipt_store)
        with self.ledger.connection() as db:
            pending = db.execute("SELECT sequence,payload FROM receipt_journal WHERE sequence>? ORDER BY sequence LIMIT 100",
                                 (int(before or 0),)).fetchall()
        if pending:
            events = [dict(json.loads(row[1]), sequence=row[0]) for row in pending]
            await self.commit(receipt_store, before, str(pending[-1][0]), events)

    async def follow(self):
        ticks = 0
        while True:
            try:
                more = await self.once()
                ticks += 1
                if ticks % 15 == 0 and self.ledger.get("capture_enabled", False):
                    await self.receipts()
                if more:
                    continue
            except (CLIError, httpx.HTTPError, RuntimeError, sqlite3.Error, ValueError, KeyError) as exc:
                self.error = exc.code if isinstance(exc, CLIError) else "capture_unavailable"
            await asyncio.sleep(self.settings.poll_seconds)
