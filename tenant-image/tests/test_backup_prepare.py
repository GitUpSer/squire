import importlib.util
from contextlib import closing
import hashlib
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


def test_live_wal_rows_become_standalone_immutable_snapshot(tmp_path):
    volume=tmp_path/"volume";volume.mkdir()
    source=volume/"state.db";destination=volume/".squire/backup-consistent/current"
    with closing(sqlite3.connect(source)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE memory(value TEXT)")
        writer.execute("INSERT INTO memory VALUES('committed only in live WAL')");writer.commit()
        wal=Path(str(source)+"-wal");assert wal.stat().st_size>0
        before=wal.read_bytes()
        backup.prepare(volume,destination)
        target=destination/"sqlite/state.db"
        assert not Path(str(target)+"-wal").exists() and not Path(str(target)+"-shm").exists()
        manifest=json.loads((destination/"manifest.json").read_text())
        assert manifest["sqlite"]["state.db"]["sha256"]==hashlib.sha256(target.read_bytes()).hexdigest()
        main_only=tmp_path/"restored.db";main_only.write_bytes(target.read_bytes())
        for _ in range(2):
            with closing(sqlite3.connect(main_only.as_uri()+"?immutable=1",uri=True)) as restored:
                assert restored.execute("SELECT value FROM memory").fetchone()[0]=="committed only in live WAL"
                assert restored.execute("PRAGMA integrity_check").fetchone()[0]=="ok"
        assert target.read_bytes()==main_only.read_bytes()
        assert wal.read_bytes()==before  # source was never checkpointed


def test_all_sqlite_handles_close_before_manifest_hash(tmp_path,monkeypatch):
    volume=tmp_path/"volume";volume.mkdir()
    with closing(sqlite3.connect(volume/"state.db")) as source:
        source.execute("CREATE TABLE messages(id INTEGER)");source.commit()
    opened=[];original=sqlite3.connect;original_digest=backup.digest
    class Tracked(sqlite3.Connection):
        def close(self):self.did_close=True;super().close()
    def connect(*args,**kwargs):
        conn=original(*args,**kwargs,factory=Tracked);conn.did_close=False;opened.append(conn);return conn
    def checked_digest(path):
        assert opened and all(connection.did_close for connection in opened)
        return original_digest(path)
    monkeypatch.setattr(backup.sqlite3,"connect",connect)
    monkeypatch.setattr(backup,"digest",checked_digest)
    backup.prepare(volume,volume/".squire/backup-consistent/current")


@pytest.mark.parametrize("version",["18.3.0","18.4.0"])
def test_pg0_uses_running_servers_versioned_sibling_tools(tmp_path,monkeypatch,version):
    volume=tmp_path/"volume";instance=volume/".pg0/instances/hindsight";data=instance/"data"
    data.mkdir(parents=True);(data/"PG_VERSION").write_text("18")
    (data/"postmaster.pid").write_text("987654\n")
    installation=volume/".pg0/installation";binary=installation/version/"bin";binary.mkdir(parents=True)
    for name in ("postgres","psql","pg_dump","pg_restore"):(binary/name).touch()
    cfg={"data_dir":str(data),"installation_dir":str(installation),"port":55432,"username":"hindsight","database":"hindsight","password":"synthetic-pg-secret"}
    (instance/"instance.json").write_text(json.dumps(cfg))
    original_resolve=Path.resolve;original_read=Path.read_bytes
    def resolve(path,*args,**kwargs):
        if path==Path("/proc/987654/exe"):return binary/"postgres"
        return original_resolve(path,*args,**kwargs)
    def read(path):
        if path==Path("/proc/987654/cmdline"):return b"postgres\0-D\0"+str(data).encode()+b"\0"
        return original_read(path)
    calls=[]
    def command(argv,env=None):
        calls.append(argv)
        assert Path(argv[0]).parent==binary
        assert cfg["password"] not in " ".join(argv)
        if argv[-1]=="SHOW data_directory":return str(data).encode()
        if "pg_database_size" in argv[-1]:return b"1024"
        if Path(argv[0]).name=="pg_dump":Path(argv[argv.index("--file")+1]).write_bytes(b"PGDMP synthetic archive")
        return b"verified archive"
    monkeypatch.setattr(Path,"resolve",resolve);monkeypatch.setattr(Path,"read_bytes",read)
    monkeypatch.setattr(backup,"command",command)
    destination=volume/".squire/backup-consistent/current"
    assert backup.prepare(volume,destination)=={"sqlite_stores":0,"postgres_databases":1}
    assert len(calls)==4 and (destination/"postgres/hindsight.dump").exists()


@pytest.mark.parametrize("metadata_present",[True,False])
def test_stopped_or_unresolved_clusters_fail_without_replacing_previous_backup(tmp_path,metadata_present):
    volume=tmp_path/"volume";instance=volume/".pg0/instances/stopped";data=instance/"data";data.mkdir(parents=True)
    (data/"PG_VERSION").write_text("18")
    if metadata_present:(instance/"instance.json").write_text(json.dumps({"data_dir":str(data)}))
    destination=volume/".squire/backup-consistent/current";destination.mkdir(parents=True)
    (destination/"preserved-proof").write_text("previous successful backup")
    with pytest.raises(RuntimeError,match="postgres_cluster_"):backup.prepare(volume,destination)
    assert (destination/"preserved-proof").read_text()=="previous successful backup"
