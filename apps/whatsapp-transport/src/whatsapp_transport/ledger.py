"""Local metadata only. Durable send intents make crash ambiguity explicit."""
import contextlib
import json
import sqlite3
import time
from pathlib import Path


class Conflict(Exception):
    pass


class Ledger:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sends(
                    operation_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                    generation INTEGER NOT NULL, state TEXT NOT NULL,
                    provider_message_id TEXT, error_code TEXT, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS captured(
                    chat_hash TEXT NOT NULL, message_hash TEXT NOT NULL,
                    captured_at REAL NOT NULL, PRIMARY KEY(chat_hash,message_hash));
            """)
        path.chmod(0o600)

    @contextlib.contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def initialize(self, generation: int, enabled: bool):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT value FROM metadata WHERE key='generation'").fetchone()
            if existing and generation < int(json.loads(existing[0])):
                raise Conflict("configured generation is older than durable fence")
            self._put(db, "generation", generation)
            # A config change can disable sends; enabling is an explicit operator action.
            if not enabled or self.get("outbound_enabled") is None:
                self._put(db, "outbound_enabled", enabled)
            db.execute("UPDATE sends SET state='uncertain', error_code='process_restarted' WHERE state='dispatching'")

    @staticmethod
    def _put(db, key, value):
        db.execute("INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, json.dumps(value)))

    def get(self, key, default=None):
        with self.connection() as db:
            row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def put(self, key, value):
        with self.connection() as db:
            self._put(db, key, value)

    def control(self, *, generation: int, outbound_enabled: bool):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = json.loads(db.execute("SELECT value FROM metadata WHERE key='generation'").fetchone()[0])
            if generation < old:
                raise Conflict("stale_generation")
            self._put(db, "generation", generation)
            self._put(db, "outbound_enabled", outbound_enabled)

    def begin_send(self, operation_id, fingerprint, generation):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM sends WHERE operation_id=?", (operation_id,)).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise Conflict("operation_payload_changed")
                return dict(old)
            db.execute("INSERT INTO sends VALUES (?,?,?,'dispatching',NULL,NULL,?)",
                       (operation_id, fingerprint, generation, time.time()))
        return None

    def finish_send(self, operation_id, state, message_id=None, error_code=None):
        with self.connection() as db:
            db.execute("UPDATE sends SET state=?,provider_message_id=?,error_code=?,updated_at=? WHERE operation_id=?",
                       (state, message_id, error_code, time.time(), operation_id))

    def retry_unsent(self, operation_id):
        with self.connection() as db:
            changed = db.execute("UPDATE sends SET state='dispatching',error_code=NULL,updated_at=? WHERE operation_id=? AND state='definitely_unsent'",
                                 (time.time(), operation_id)).rowcount
            if changed != 1:
                raise Conflict("retry_not_proven_unsent")

    def capture_batch(self, checkpoint, records):
        with self.connection() as db:
            self._put(db, "checkpoint", checkpoint)
            db.executemany("INSERT OR REPLACE INTO captured VALUES (?,?,?)", records)

    def was_captured(self, chat_hash, message_hash):
        with self.connection() as db:
            return db.execute("SELECT 1 FROM captured WHERE chat_hash=? AND message_hash=?", (chat_hash, message_hash)).fetchone() is not None
