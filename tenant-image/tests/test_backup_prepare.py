import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("backup_prepare", Path(__file__).parents[1] / "bin/squire-backup-prepare.py")
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


def test_consistent_sqlite_snapshot_excludes_live_files_and_skips_secret_symlink(tmp_path):
    volume = tmp_path / "volume"
    volume.mkdir()
    source = volume / "state.db"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY,text TEXT)")
        db.execute("INSERT INTO messages VALUES (1,'private transcript')")
    secret = tmp_path / "secret.env"
    secret.write_text("MODEL_SECRET=sentinel")
    (volume / ".env").symlink_to(secret)
    destination = volume / ".squire/backup-consistent/current"
    assert backup.prepare(volume, destination) == {"sqlite_stores": 1, "postgres_databases": 0}
    assert not (destination / "sqlite/.env").exists()
    with sqlite3.connect(destination / "sqlite/state.db") as db:
        assert db.execute("SELECT text FROM messages").fetchone()[0] == "private transcript"
    excluded = (destination / "live-excludes.txt").read_text().splitlines()
    assert str(source) in excluded and str(source) + "-wal" in excluded
    assert backup.prepare(volume, destination)["sqlite_stores"] == 1  # never backs up its own staging


def test_configured_pg_cannot_silently_fall_back_to_raw_copy(tmp_path, monkeypatch):
    volume = tmp_path / "volume"
    volume.mkdir()
    monkeypatch.setenv("HINDSIGHT_API_DATABASE_URL", "pg0://hindsight:5432")
    with pytest.raises(RuntimeError, match="hindsight_not_backed_up"):
        backup.prepare(volume, volume / ".squire/backup-consistent/current")


def test_staging_must_stay_inside_volume(tmp_path):
    volume = tmp_path / "volume"
    volume.mkdir()
    with pytest.raises(RuntimeError, match="unsafe_backup_location"):
        backup.prepare(volume, tmp_path / "elsewhere")
