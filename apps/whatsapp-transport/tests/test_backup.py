import asyncio
import contextlib
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "infra"))

from migrate_personal_agent import offline_sqlite
from pilot_backup_worker import verify_ops_tree
from whatsapp_transport.backup import snapshot_transport


async def test_wal_snapshot_archive_has_only_verified_standalone_files(tmp_path, monkeypatch):
    store = tmp_path / "wacli"
    store.mkdir()
    for path in (store / "wacli.db", store / "session.db", tmp_path / "transport.sqlite3"):
        with offline_sqlite(path) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE fixture(id INTEGER)")
            db.execute("INSERT INTO fixture VALUES(1)")

    def encrypt(stage, archive, recipient):
        assert verify_ops_tree(stage)["file_count"] == 3
        assert verify_ops_tree(stage)["file_count"] == 3
        for name in ("wacli.db", "session.db", "transport.sqlite3"):
            with contextlib.closing(sqlite3.connect(f"file:{stage / name}?immutable=1", uri=True)) as db:
                assert db.execute("SELECT id FROM fixture").fetchall() == [(1,)]
        archive.write_bytes(b"synthetic encrypted output")

    monkeypatch.setattr("migrate_personal_agent.encrypt_tree", encrypt)
    @contextlib.asynccontextmanager
    async def exclusive():
        yield
    settings = SimpleNamespace(data_dir=tmp_path, store_dir=store, backup_recipient="age1synthetic", provider_account_id="pilot")
    capture = SimpleNamespace(lock=asyncio.Lock(), ledger=SimpleNamespace(get=lambda key: 1))
    root, archive = await snapshot_transport(settings, SimpleNamespace(exclusive=exclusive), capture)
    assert archive.is_file()
    shutil.rmtree(root)
