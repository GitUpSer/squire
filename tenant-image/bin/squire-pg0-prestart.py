#!/usr/bin/env python3
"""Fence pg0 startup and archive only proven stale clean-stop metadata.

pg0's PID-only liveness check can mistake a reused Node PID for PostgreSQL.
Never signal that PID or remove postmaster.pid, database files, or WAL. Crash
recovery with a remaining postmaster.pid requires explicit operator inspection.
"""
import fcntl
import importlib.metadata
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid


class StartupError(RuntimeError):
    pass


def require(condition, reason):
    if not condition:
        raise StartupError(reason)


def safe_path(path):
    require(path.is_absolute() and ".." not in path.parts, "unsafe_pg0_path")
    for item in (path, *path.parents):
        require(not item.is_symlink(), "pg0_symlink_forbidden")
    return path


def instance_config(home, url, pg_version, api_version):
    require((api_version, pg_version) in {("0.5.2", "0.12.3"), ("0.9.0", "0.15.1")}, "unreviewed_pg0_version")
    if url != "pg0" and not url.startswith("pg0://"):
        return None
    raw = "hindsight" if url == "pg0" else url[6:]
    if api_version == "0.9.0":
        raw = raw.rsplit("@", 1)[-1]
    name, sep, port = raw.rpartition(":")
    if not sep:
        name, port = raw, None
    else:
        require(port.isdecimal() and 1 <= int(port) <= 65535, "unsafe_pg0_port")
        port = int(port)
    name = name or "hindsight"
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", name), "unsafe_pg0_instance")
    home = safe_path(Path(home))
    return safe_path(home/".pg0/instances"/name), port


def process(pid):
    """Return verified executable/argv for a live PID, or None if gone/zombie."""
    proc = Path("/proc")/str(pid)
    try:
        before = (proc/"stat").read_text().rsplit(")", 1)[1].split()
        if before[0] == "Z":
            return None
        executable = (proc/"exe").resolve(strict=True)
        argv = (proc/"cmdline").read_bytes().split(b"\0")
        after = (proc/"stat").read_text().rsplit(")", 1)[1].split()
        if after[0] == "Z":
            return None
        require(before[19] == after[19], "pg0_pid_changed_during_inspection")
        require(argv and argv[0], "pg0_process_identity_unreadable")
        return executable, argv
    except FileNotFoundError:
        # During exit Linux can remove exe/cmdline before the zombie's /proc
        # directory disappears. Re-read status; disappearance alone is not
        # sufficient evidence when the process directory is still present.
        try:
            current = (proc/"stat").read_text().rsplit(")", 1)[1].split()
            if current[0] == "Z":
                return None
        except FileNotFoundError:
            require(not proc.exists(), "pg0_process_identity_ambiguous")
            return None
        except (PermissionError, ProcessLookupError, ValueError, IndexError):
            raise StartupError("pg0_process_identity_unreadable") from None
        raise StartupError("pg0_process_identity_ambiguous") from None
    except (PermissionError, ProcessLookupError, ValueError, IndexError):
        raise StartupError("pg0_process_identity_unreadable") from None


def no_live_cluster(data):
    # A removed PID file alone is not evidence that the cluster stopped. Inspect
    # same-user PostgreSQL processes before permitting pg0 to adopt this data.
    for proc in Path("/proc").iterdir():
        if not proc.name.isdecimal():
            continue
        try:
            if proc.stat().st_uid != os.geteuid():
                continue
            found = process(int(proc.name))
            if found is None or found[0].name != "postgres":
                continue
            _, argv = found
            try:
                cwd = (proc/"cwd").resolve(strict=True)
            except OSError:
                raise StartupError("postgres_cluster_identity_unreadable") from None
            require(cwd != data, "live_postgres_cluster_requires_operator")
            require((cwd/"PG_VERSION").is_file(), "postgres_cluster_identity_unreadable")
            # Postmaster carries -D; server children normally carry the data cwd.
            for index, arg in enumerate(argv):
                if arg == b"-D" and index+1 < len(argv):
                    require(Path(os.fsdecode(argv[index+1])) != data, "live_postgres_cluster_requires_operator")
                if arg.startswith(b"--pgdata="):
                    require(Path(os.fsdecode(arg.split(b"=", 1)[1])) != data, "live_postgres_cluster_requires_operator")
        except FileNotFoundError:
            continue
        except PermissionError:
            raise StartupError("postgres_process_inspection_denied") from None


def expected_credentials(url, api_version):
    username = password = "hindsight"
    if api_version == "0.9.0" and "@" in url:
        credentials = url[6:].rsplit("@", 1)[0]
        user, sep, secret = credentials.partition(":")
        username = user or username
        if sep:
            password = secret
    return username, password


def live_cluster(record, data, pidfile, installation):
    """Reuse a native PostgreSQL only when every independent owner agrees."""
    safe_path(pidfile)
    lines = pidfile.read_text().splitlines()
    require(len(lines) >= 8 and lines[7].strip() == "ready", "postmaster_pid_requires_operator_inspection")
    require(lines[0] == str(record["pid"]) and lines[1] == str(data) and lines[3] == str(record["port"]), "postmaster_identity_mismatch")
    found = process(record["pid"])
    require(found is not None, "postmaster_process_missing_or_stale")
    executable, argv = found
    candidates = [safe_path(path) for path in installation.rglob("postgres") if path.is_file()]
    require(len(candidates) == 1 and executable == candidates[0], "postmaster_executable_mismatch")
    targets = []
    for index, arg in enumerate(argv):
        if arg == b"-D" and index+1 < len(argv):
            targets.append(os.fsdecode(argv[index+1]))
        elif arg.startswith(b"--pgdata="):
            targets.append(os.fsdecode(arg.split(b"=", 1)[1]))
    if not targets:
        try:
            targets = [os.fsdecode(item.split(b"=", 1)[1]) for item in
                       (Path("/proc")/str(record["pid"])/"environ").read_bytes().split(b"\0") if item.startswith(b"PGDATA=")]
        except OSError:
            raise StartupError("postmaster_data_directory_unreadable") from None
    require(targets == [str(data)], "postmaster_data_directory_mismatch")
    require(pidfile.read_text().splitlines() == lines, "postmaster_identity_changed")


def repair(home, database_url, pg0_version, hindsight_version):
    """Called under the startup lock; returns a status without exposing metadata."""
    config = instance_config(home, database_url, pg0_version, hindsight_version)
    if config is None:
        return "external_database"
    directory, port = config
    data = safe_path(directory/"data")
    metadata = safe_path(directory/"instance.json")
    pidfile = safe_path(data/"postmaster.pid")
    if not metadata.exists():
        require(not pidfile.exists(), "postmaster_pid_without_metadata_requires_operator")
        if data.exists():
            no_live_cluster(data)
        return "no_instance_metadata"
    require(data.is_dir(), "pg0_data_directory_missing")
    version_file = safe_path(data/"PG_VERSION")
    require(version_file.is_file() and version_file.read_text().strip() == "18", "pg0_data_version_unexpected")
    fd = os.open(metadata, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        original = os.fstat(fd)
        require(stat.S_ISREG(original.st_mode) and original.st_nlink == 1, "unsafe_pg0_metadata_file")
        with os.fdopen(os.dup(fd)) as stream:
            record = json.load(stream)
        require(record.get("data_dir") == str(data), "pg0_metadata_data_directory_mismatch")
        require(record.get("installation_dir") == str(Path(home)/".pg0/installation"), "pg0_metadata_installation_mismatch")
        installation = safe_path(Path(record["installation_dir"]))
        username, password = expected_credentials(database_url, hindsight_version)
        require((record.get("username"),record.get("password"),record.get("database")) ==
                (username,password,"hindsight"), "pg0_metadata_credentials_mismatch")
        require(type(record.get("pid")) is int and record["pid"] > 1, "invalid_pg0_metadata_pid")
        require(type(record.get("port")) is int and 1 <= record["port"] <= 65535 and (port is None or record["port"] == port), "pg0_metadata_port_mismatch")
        if pidfile.exists():
            live_cluster(record, data, pidfile, installation)
            current = metadata.lstat()
            require((current.st_dev,current.st_ino,current.st_size,current.st_mtime_ns) ==
                    (original.st_dev,original.st_ino,original.st_size,original.st_mtime_ns), "pg0_metadata_changed_during_inspection")
            return "existing_postgres_identity_verified"
        found = process(record["pid"])
        require(found is None or found[0].name != "postgres", "postgres_pid_requires_operator_inspection")
        no_live_cluster(data)
        # Recheck after process inspection. Never replace another generation's
        # metadata, and never allow upstream to remove a newly appeared PID file.
        require(not pidfile.exists() and not pidfile.is_symlink(), "postmaster_pid_appeared_during_inspection")
        current = metadata.lstat()
        require((current.st_dev,current.st_ino,current.st_size,current.st_mtime_ns) ==
                (original.st_dev,original.st_ino,original.st_size,original.st_mtime_ns), "pg0_metadata_changed_during_inspection")
        archive = directory/("instance.stale-"+uuid.uuid4().hex+".json")
        os.fchmod(fd, 0o600)
        os.link(metadata, archive, follow_symlinks=False)
        metadata.unlink()
        syncfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(syncfd)
        finally:
            os.close(syncfd)
        return "stale_instance_metadata_archived"
    finally:
        os.close(fd)


def main():
    require(len(sys.argv) >= 2, "hindsight_executable_required")
    home = Path(os.environ["HOME"])
    url = os.environ.get("HINDSIGHT_API_DATABASE_URL", "pg0")
    if url != "pg0" and not url.startswith("pg0://"):
        os.execv(sys.argv[1], sys.argv[1:])
    pg_version = importlib.metadata.version("pg0-embedded")
    api_version = importlib.metadata.version("hindsight-api-slim")
    config = instance_config(home, url, pg_version, api_version)
    if config is not None:
        directory, _ = config
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = os.open(safe_path(directory/".squire-startup.lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StartupError("another_hindsight_instance_holds_startup_lock") from None
        # Hindsight owns this descriptor until exit. subprocess.run/Popen close
        # unrelated descriptors by default, so pg0/Postgres do not inherit it.
        os.set_inheritable(lock, True)
    result = repair(home, url, pg_version, api_version)
    print("[squire-pg0-prestart] "+result, flush=True)
    os.execv(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        reason = str(exc) if isinstance(exc, StartupError) else "pg0_prestart_inspection_failed"
        print("[squire-pg0-prestart] "+reason, file=sys.stderr)
        raise SystemExit(1) from None
