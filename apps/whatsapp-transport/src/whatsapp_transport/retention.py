"""Pinned-schema local tombstones + upstream payload purge, never WhatsApp deletion.

The journal must acknowledge the tombstone BEFORE purge removes old references.
This ensures the retained floor cannot overtake the committed checkpoint.
"""
import asyncio
import sqlite3
import time

from .capture import digest


class Retention:
    def __init__(self, settings, cli, ledger, supervisor, capture):
        self.settings, self.cli, self.ledger = settings, cli, ledger
        self.supervisor, self.capture = supervisor, capture

    async def run(self, days=7, limit=100, dry_run=True):
        if not self.ledger.get("capture_enabled", False):
            raise ValueError("capture_required")
        async with self.supervisor.exclusive():
            while await self.capture.once():
                pass
            path = self.settings.store_dir / "wacli.db"
            cursor = self.ledger.get("retention_scan_cursor", [-9223372036854775808, 0])
            eligible, scanned, skipped = [], 0, 0
            finished = False
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
                # Expiry and legacy quarantine bypass the age cursor. New tombstones
                # are acknowledged even when the payload predates activation.
                eligible = self.expired(db, limit)
                # Advance over ineligible legacy history; an oldest-page LIMIT must
                # never starve captured records behind the activation baseline.
                while scanned < 1000 and len(eligible) < limit:
                    rows = db.execute("""SELECT chat_jid,msg_id,ts,rowid FROM messages
                        WHERE ts<? AND payload_purged_at IS NULL AND (ts>? OR (ts=? AND rowid>?))
                        ORDER BY ts,rowid LIMIT ?""", (int(time.time()) - days * 86400,
                        cursor[0], cursor[0], cursor[1], min(100, 1000 - scanned))).fetchall()
                    if not rows:
                        finished = True
                        break
                    for chat, msg, timestamp, rowid in rows:
                        cursor = [timestamp, rowid]
                        scanned += 1
                        if (chat, msg) in eligible:
                            continue
                        if self.ledger.was_captured(digest(chat), digest(msg)):
                            eligible.append((chat, msg))
                        else:
                            skipped += 1
                        if len(eligible) >= limit:
                            break
            if dry_run:
                return {"eligible": len(eligible), "skipped_uncaptured": skipped, "scanned": scanned, "purged": 0}
            # Do not call `messages delete --for-me`: it transmits a device-side deletion.
            # These local updates execute the verified v0.26.0 journal/FTS triggers.
            with sqlite3.connect(path) as db:
                db.execute("PRAGMA synchronous=FULL")
                db.executemany("UPDATE messages SET deleted_at=COALESCE(deleted_at,?),deletion_reason=COALESCE(deletion_reason,'squire-retention') WHERE chat_jid=? AND msg_id=?",
                               [(int(time.time()), chat, msg) for chat, msg in eligible])
            while await self.capture.once():
                pass
            purged = 0
            for chat, msg in eligible:
                await self.cli.run("messages", "purge", "--chat", chat, "--id", msg, "--confirm")
                purged += 1
            while await self.capture.once():
                pass
            self.ledger.put("retention_scan_cursor", [-9223372036854775808, 0] if finished else cursor)
            return {"eligible": len(eligible), "skipped_uncaptured": skipped, "scanned": scanned, "purged": purged}

    @staticmethod
    def expired(db, limit):
        return db.execute("""SELECT e.chat_jid,e.msg_id FROM squire_message_expiry e
            JOIN messages m ON m.chat_jid=e.chat_jid AND m.msg_id=e.msg_id
            WHERE (e.known=0 OR e.expires_at<=?) AND m.payload_purged_at IS NULL
            ORDER BY e.known DESC,e.expires_at LIMIT ?""", (int(time.time()), limit)).fetchall()

    async def follow(self):
        next_regular = time.monotonic() + self.settings.retention_interval_seconds
        while True:
            await asyncio.sleep(min(5, self.settings.retention_interval_seconds))
            if not self.settings.retention_enabled or not self.ledger.get("capture_enabled", False):
                continue
            try:
                if time.monotonic() < next_regular:
                    path = self.settings.store_dir / "wacli.db"
                    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
                        if not self.expired(db, 1):
                            continue
                result = await self.run(days=self.settings.retention_days, limit=100, dry_run=False)
                next_regular = time.monotonic() + self.settings.retention_interval_seconds
                self.ledger.put("retention_status", {"at": time.time(), **result})
            except Exception:
                # Never advance a cursor or force a purge after any capture/CLI failure.
                self.ledger.put("retention_status", {"at": time.time(), "error": "retention_failed"})
