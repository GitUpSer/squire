#!/usr/bin/env python3
"""Prepare consistent SQLite and logical PostgreSQL copies for tenant restic."""
import argparse
from contextlib import closing
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path


def require(value, reason):
    if not value:
        raise RuntimeError(reason)


def command(argv, env=None):
    result = subprocess.run(argv, env=env, capture_output=True, timeout=1800, check=False)
    require(result.returncode == 0, "database_command_failed")
    return result.stdout


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            result.update(chunk)
    return result.hexdigest()


def sqlite_snapshot(source, target, progress):
    # SQLite's context manager commits, but does not close. Finish the offline
    # destination in its main file before hashing; never checkpoint the source.
    with closing(sqlite3.connect(source.as_uri()+"?mode=ro",uri=True)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst,pages=256,sleep=0.05,progress=progress)
        dst.commit()
        status=dst.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        require(status is not None and status[0]==0,"sqlite_snapshot_checkpoint_busy")
        require(dst.execute("PRAGMA journal_mode=DELETE").fetchone()[0].lower()=="delete","sqlite_snapshot_not_standalone")
        require(dst.execute("PRAGMA integrity_check").fetchone()[0]=="ok","sqlite_integrity_failed")
    require(not Path(str(target)+"-wal").exists() and not Path(str(target)+"-shm").exists(),"sqlite_snapshot_sidecars_remain")


def postgres_snapshot(volume, instance_file, stage):
    require(not instance_file.is_symlink(), "unsafe_postgres_config")
    cfg = json.loads(instance_file.read_text())
    data = Path(cfg["data_dir"]).resolve()
    require(data == (instance_file.parent / "data").resolve(), "postgres_data_path_mismatch")
    require((data/"postmaster.pid").is_file(),"postgres_cluster_not_running_no_snapshot")
    lines = (data / "postmaster.pid").read_text().splitlines()
    pid = int(lines[0])
    executable = (Path("/proc") / str(pid) / "exe").resolve(strict=True)
    args = (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0")
    require(executable.name == "postgres" and str(data).encode() in args, "postgres_process_mismatch")
    installation=Path(cfg["installation_dir"]).resolve(strict=True)
    require(installation==(volume/".pg0/installation").resolve() and executable.is_relative_to(installation),"postgres_installation_mismatch")
    # pg0 metadata names its installation root, while binaries are nested under
    # the bundled PostgreSQL version. Use the verified running server's siblings.
    binary = executable.parent
    require(all((binary/name).is_file() and (binary/name).resolve().parent==binary for name in ("psql","pg_dump","pg_restore")),"matching_postgres_tools_missing")
    major = (data / "PG_VERSION").read_text().strip()
    require(major == "18", "postgres_major_unreviewed")
    env = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    env.update(PGHOST="127.0.0.1", PGPORT=str(cfg["port"]), PGUSER=cfg["username"], PGDATABASE=cfg["database"],
               PGPASSWORD=cfg["password"], PGCONNECT_TIMEOUT="10")
    actual = command([str(binary / "psql"), "--no-password", "--no-psqlrc", "-Atc", "SHOW data_directory"], env).decode().strip()
    require(actual == str(data), "postgres_connection_mismatch")
    database_bytes = int(command([str(binary / "psql"), "--no-password", "--no-psqlrc", "-Atc", "SELECT pg_database_size(current_database())"], env).decode().strip())
    require(shutil.disk_usage(volume).free > database_bytes * 1.2 + 128 * 1024**2, "postgres_backup_space_insufficient")
    target = stage / "postgres" / (instance_file.parent.name + ".dump")
    target.parent.mkdir(exist_ok=True, mode=0o700)
    command([str(binary / "pg_dump"), "--no-password", "--format=custom", "--no-owner", "--no-acl", "--file", str(target)], env)
    command([str(binary / "pg_restore"), "--list", str(target)])
    target.chmod(0o600)
    return {"major": major, "database": cfg["database"], "sha256": digest(target)},str(data)


def prepare(volume: Path, destination: Path):
    volume, destination = volume.resolve(), destination.resolve()
    require(destination.is_relative_to(volume) and not volume.is_relative_to(Path("/dev/shm")), "unsafe_backup_location")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    excluded = []
    manifest = {"format": "squire-runtime-consistent-backup-v1", "created_at": time.time(), "sqlite": {}, "postgres": {}}
    with tempfile.TemporaryDirectory(prefix="prepare-", dir=destination.parent) as temporary:
        stage = Path(temporary)
        for current, directories, names in os.walk(volume, followlinks=False):
            current = Path(current)
            directories[:] = [name for name in directories if name not in {".pg0", ".git", "node_modules", "logs", "cache", ".cache"}
                              and not (current / name).is_symlink() and not (current / name).resolve().is_relative_to(destination.parent)]
            for name in names:
                source = current / name
                if source.is_symlink() or not source.is_file() or name.endswith(("-wal", "-shm")):
                    continue
                with source.open("rb") as stream:
                    if stream.read(16) != b"SQLite format 3\0":
                        continue
                relative = str(source.relative_to(volume))
                require("\n" not in str(source), "invalid_database_path")
                require(shutil.disk_usage(volume).free > source.stat().st_size * 1.2 + 128 * 1024**2, "sqlite_backup_space_insufficient")
                target = stage / "sqlite" / relative
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                deadline = time.monotonic() + 900
                def progress(status, remaining, total):
                    require(time.monotonic() < deadline, "sqlite_backup_timeout")
                sqlite_snapshot(source,target,progress)
                target.chmod(0o600)
                excluded.extend([str(source), str(source) + "-wal", str(source) + "-shm"])
                manifest["sqlite"][relative] = {"sha256": digest(target), "sqlite_version": sqlite3.sqlite_version}
        instances=volume/".pg0/instances"
        for cluster in instances.glob("*/data"):
            require((cluster.parent/"instance.json").is_file(),"postgres_cluster_metadata_missing_no_snapshot")
        for instance_file in sorted(instances.glob("*/instance.json")):
            metadata,data=postgres_snapshot(volume,instance_file,stage)
            manifest["postgres"][instance_file.parent.name]=metadata
            excluded.append(data)
        if os.environ.get("HINDSIGHT_API_DATABASE_URL", "").startswith("pg0://"):
            require(manifest["postgres"], "configured_hindsight_not_backed_up")
        (stage / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
        (stage / "live-excludes.txt").write_text("\n".join(excluded) + "\n")
        for item in (stage / "manifest.json", stage / "live-excludes.txt"):
            item.chmod(0o600)
        previous = destination.with_name("previous")
        require(not previous.is_symlink(), "unsafe_previous_backup")
        if previous.exists():
            shutil.rmtree(previous)
        if destination.exists():
            destination.rename(previous)
        stage.rename(destination)
        if previous.exists():
            shutil.rmtree(previous)
    return {"sqlite_stores": len(manifest["sqlite"]), "postgres_databases": len(manifest["postgres"])}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--volume", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    arguments = parser.parse_args()
    os.umask(0o077)
    try:
        print(json.dumps(prepare(arguments.volume, arguments.destination)))
    except Exception:
        raise SystemExit("consistent database backup failed; no raw live database backup was attempted") from None
