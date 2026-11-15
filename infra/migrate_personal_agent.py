#!/usr/bin/env python3
"""Explicit, fail-closed founder migration tools. No default remote mutation."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

from pilot_inventory import UNITS, postgres_inventory, service_inventory, sqlite_inventory

FORMAT = "squire-founder-snapshot-v1"
STATE_SCHEMA = "5bfa84adc1bfd8cbf5427a830707c82e2f63adcaf42ac3f9a51cdb5fb905cfbf"
SOURCE_RELEASE = "v2026.9.24__be3f3d797"
GATES = {"restored_image", "private_memory_parity", "scheduler_translation", "identity_map",
         "group_isolation", "media_parity", "first_contact", "group_roundtrip", "rollback_rehearsal"}
FENCE_UNITS = ("hermes-stall-recovery.service", "hindsight-healthcheck.timer", "hermes-gateway.service",
               "hindsight-daemon.service", "camofox-server.service")
SAFE_PART = re.compile(r"^[A-Za-z0-9._-]{1,150}$")


class MigrationError(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise MigrationError(code)


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def checksum(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def safe_path(root: Path, relative: str):
    path = Path(relative)
    require(not path.is_absolute() and bool(path.parts) and all(p not in {"..", "."} for p in path.parts), "unsafe_relative_path")
    target = root / path
    require(target.resolve().is_relative_to(root.resolve()), "path_escapes_root")
    for parent in [target, *target.parents]:
        if parent == root.parent:
            break
        require(not parent.is_symlink(), "symlink_not_permitted")
    return target


def run(argv, *, env=None, timeout=60, stdin=None):
    try:
        result = subprocess.run(argv, env=env, input=stdin, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise MigrationError("command_unavailable_or_timeout") from None
    require(result.returncode == 0, "command_failed")
    return result.stdout


def backup_sqlite(source: Path, destination: Path, expected_schema: str, timeout=900):
    require(sqlite_inventory(source).get("schema_sha256") == expected_schema, "source_schema_changed")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    deadline = time.monotonic() + timeout
    def progress(status, remaining, total):
        require(time.monotonic() < deadline, "sqlite_backup_timeout")
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=5) as src, sqlite3.connect(destination) as target:
        src.backup(target, pages=256, progress=progress, sleep=0.05)
        require(target.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "sqlite_backup_corrupt")
    destination.chmod(0o600)
    require(sqlite_inventory(destination).get("schema_sha256") == expected_schema, "backup_schema_changed")


def pg_environment(config, home=None):
    env = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    for key, name in (("host", "PGHOST"), ("port", "PGPORT"), ("user", "PGUSER"), ("database", "PGDATABASE")):
        require(config.get(key) is not None, "postgres_identity_missing")
        env[name] = str(config[key])
    env["PGCONNECT_TIMEOUT"] = "10"
    secret_name = config.get("password_env")
    if secret_name:
        require(re.fullmatch(r"[A-Z][A-Z0-9_]+", secret_name) and secret_name in os.environ, "postgres_password_missing")
        env["PGPASSWORD"] = os.environ[secret_name]
    elif config.get("credential_source") == "instance_config":
        require(home is not None, "source_home_required")
        credentials = safe_path(home, ".pg0/instances/hindsight-embed-hermes/instance.json")
        value = json.loads(credentials.read_text())
        require(str(value.get("data_dir")) == config["data_dir"]
                and str(value.get("port")) == str(config["port"])
                and value.get("username") == config["user"] and value.get("database") == config["database"],
                "postgres_credential_identity_mismatch")
        require(isinstance(value.get("password"), str) and bool(value["password"]), "postgres_password_missing")
        env["PGPASSWORD"] = value["password"]
    elif config.get("credential_source") == "daemon_environment":
        pid = int(run(["systemctl", "--user", "show", "hindsight-daemon.service", "--property=MainPID", "--value"]).decode().strip())
        raw = (Path("/proc") / str(pid) / "environ").read_bytes()
        values = dict(item.split(b"=", 1) for item in raw.split(b"\0") if b"=" in item)
        url = urlsplit(values[b"HINDSIGHT_API_DATABASE_URL"].decode())
        require(url.scheme in {"postgres", "postgresql"} and url.hostname == config["host"]
                and (url.port or 5432) == int(config["port"]) and unquote(url.username or "") == config["user"]
                and unquote(url.path.lstrip("/")) == config["database"], "postgres_daemon_identity_mismatch")
        env["PGPASSWORD"] = unquote(url.password or "")
    return env


def dump_postgres(home: Path, config, destination: Path):
    active = [instance for instance in postgres_inventory(home) if instance.get("live_process_matches")]
    require(any(instance["data_dir"] == config["data_dir"] and str(instance["port"]) == str(config["port"])
                and instance["major_version"] == "18" for instance in active), "postgres_instance_not_verified")
    binary = Path(config["binary_dir"])
    env = pg_environment(config, home)
    version = run([str(binary / "pg_dump"), "--version"], env=env).decode()
    require(re.search(r"\b18\.", version) is not None, "postgres_dump_major_mismatch")
    identity = run([str(binary / "psql"), "--no-psqlrc", "--no-password", "--tuples-only", "--no-align",
                    "--command", "SELECT current_setting('data_directory') || E'\\t' || current_setting('server_version_num')"], env=env).decode().strip().split("\t")
    require(len(identity) == 2 and identity[0] == config["data_dir"] and int(identity[1]) // 10000 == 18,
            "postgres_connected_instance_mismatch")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    run([str(binary / "pg_dump"), "--no-password", "--format=custom", "--no-owner", "--no-acl", "--file", str(destination)],
        env=env, timeout=1800)
    destination.chmod(0o600)
    run([str(binary / "pg_restore"), "--list", str(destination)], timeout=60)


def copy_regular(source: Path, destination: Path):
    require(source.is_file() and not source.is_symlink(), "snapshot_requires_regular_file")
    before = source.stat()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copyfile(source, destination, follow_symlinks=False)
    destination.chmod(0o600)
    after = source.stat()
    require((before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns), "source_file_changed_during_copy")


def make_plan(home: Path, postgres, *, migration_id=None, maximum_files=100000):
    """Explicit private allowlist: omit caches/releases/logs and enumerate every kept file."""
    omitted = {".git", "node_modules", ".venv", "venv", "cache", ".cache", "releases", "logs", "__pycache__", "backups", "state-backups", "update-logs"}
    files, databases, symlinks, size = [], {}, [], 0
    for name in (".hermes", ".hindsight"):
        root = home / name
        if not root.exists():
            continue
        for current, directories, names in os.walk(root, followlinks=False):
            current = Path(current)
            kept = []
            for dirname in directories:
                item = current / dirname
                if item.is_symlink():
                    symlinks.append(str(item.relative_to(home)))
                elif dirname not in omitted:
                    kept.append(dirname)
            directories[:] = kept
            for filename in sorted(names):
                item = current / filename
                relative = str(item.relative_to(home))
                if item.is_symlink():
                    symlinks.append(relative)
                    continue
                if not item.is_file() or filename.endswith(("-wal", "-shm", ".sock", ".pid", ".lock")):
                    continue
                require(len(files) + len(databases) < maximum_files, "snapshot_manifest_file_limit")
                with item.open("rb") as stream:
                    is_sqlite = stream.read(16) == b"SQLite format 3\0"
                if is_sqlite:
                    schema = sqlite_inventory(item).get("schema_sha256")
                    require(schema is not None, "snapshot_sqlite_unreadable")
                    databases[relative] = schema
                else:
                    files.append(relative)
                size += item.stat().st_size
    require(databases.get(".hermes/state.db") == STATE_SCHEMA, "unreviewed_state_schema")
    return dict(migration_id=migration_id or str(uuid.uuid4()), scope="founder_private", source_release=SOURCE_RELEASE,
                files=sorted(files), sqlite=databases, postgres=postgres, session_map=[],
                omitted_symlinks=sorted(symlinks), estimated_plaintext_bytes=size,
                omitted_directory_names=sorted(omitted), requires_symlink_parity_review=True)


def upload_archive(archive: Path, migration_id: str):
    """Upload ciphertext only; credentials live in process environment, never output."""
    require(SAFE_PART.fullmatch(migration_id) and archive.suffix == ".age", "encrypted_archive_required")
    require(archive.open("rb").read(21).startswith(b"age-encryption.org/v1"), "age_ciphertext_required")
    try:
        import boto3
    except ImportError:
        raise MigrationError("install_boto3_for_backup_upload") from None
    names = {key: os.environ.get("BACKUP_S3_" + key) for key in ("ENDPOINT", "BUCKET", "REGION", "ACCESS_KEY_ID", "SECRET_ACCESS_KEY")}
    require(all(names.values()) and names["ENDPOINT"].startswith("https://"), "backup_bucket_configuration_missing")
    digest = checksum(archive)
    key = f"founder/{migration_id}/{digest}.age"
    client = boto3.client("s3", endpoint_url=names["ENDPOINT"], region_name=names["REGION"],
                          aws_access_key_id=names["ACCESS_KEY_ID"], aws_secret_access_key=names["SECRET_ACCESS_KEY"])
    client.upload_file(str(archive), names["BUCKET"], key,
        ExtraArgs={"ContentType": "application/octet-stream", "Metadata": {"sha256": digest, "retention-days": "30"}})
    head = client.head_object(Bucket=names["BUCKET"], Key=key)
    require(head.get("Metadata", {}).get("sha256") == digest and head["ContentLength"] == archive.stat().st_size,
            "backup_upload_verification_failed")
    return {"object_key": key, "sha256": digest, "encrypted_bytes": archive.stat().st_size,
            "requires_download_restore_drill": True, "requires_bucket_expiry_policy_days": 30}


def encrypt_tree(stage: Path, output: Path, recipient: str):
    require(recipient.startswith(("age1", "ssh-ed25519 ", "ssh-rsa ")), "age_public_recipient_required")
    require(shutil.which("age") is not None, "age_not_installed")
    require(not output.exists(), "archive_already_exists")
    temporary = output.with_name(output.name + ".partial")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as ciphertext:
            process = subprocess.Popen(["age", "--encrypt", "--recipient", recipient], stdin=subprocess.PIPE,
                                       stdout=ciphertext, stderr=subprocess.DEVNULL)
            try:
                with tarfile.open(fileobj=process.stdin, mode="w|") as tar:
                    for path in sorted(stage.rglob("*")):
                        if path.is_file():
                            require(not path.is_symlink(), "snapshot_symlink")
                            tar.add(path, arcname=str(path.relative_to(stage)), recursive=False)
                process.stdin.close()
                require(process.wait(timeout=1800) == 0, "encryption_failed")
            except BaseException:
                process.kill()
                process.wait()
                raise
            ciphertext.flush()
            os.fsync(ciphertext.fileno())
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def snapshot(home: Path, plan, output: Path, recipient: str, *, final=False, journal=None):
    require(shutil.which("age") is not None, "age_not_installed")
    require(plan.get("source_release") == SOURCE_RELEASE, "unreviewed_source_version")
    actual = (home / ".hermes/hermes-agent").resolve().name
    require(actual == plan["source_release"], "source_release_changed")
    require(plan.get("scope") == "founder_private", "full_snapshot_must_be_private")
    if final:
        require(journal is not None and journal.state["stage"] == "source_fenced", "final_snapshot_requires_fence")
        assert_fenced(home, journal)
    require(bool(plan.get("sqlite")) and bool(plan.get("files")) and bool(plan.get("postgres")), "incomplete_snapshot_plan")
    sources = [(relative, safe_path(home, relative)) for relative in plan["files"]]
    require(all(not str(Path(relative)).startswith(".pg0/") for relative, _ in sources), "postgres_requires_logical_dump")
    # Explicit file manifests make skipped caches and unapproved external paths reviewable.
    require(all(path.is_file() for _, path in sources), "snapshot_file_missing")
    estimate = sum(path.stat().st_size for _, path in sources) + sum(safe_path(home, p).stat().st_size for p in plan["sqlite"])
    require(shutil.disk_usage(output.parent).free > estimate * 2 + 512 * 1024 * 1024, "insufficient_snapshot_space")
    with tempfile.TemporaryDirectory(prefix="squire-snapshot-", dir=output.parent) as temporary:
        stage = Path(temporary)
        stage.chmod(0o700)
        for relative, source in sources:
            require(relative not in plan["sqlite"] and not relative.endswith(("-wal", "-shm")), "sqlite_must_use_backup_api")
            copy_regular(source, safe_path(stage, relative))
        for relative, schema in plan["sqlite"].items():
            backup_sqlite(safe_path(home, relative), safe_path(stage, relative), schema)
        dump_postgres(home, plan["postgres"], stage / "postgres/hindsight.dump")
        manifest = {"format": FORMAT, "migration_id": plan["migration_id"], "scope": "founder_private",
                    "source_release": actual, "created_at": time.time(), "final": final,
                    "consistency": "writers_fenced" if final else "per_database_online_rehearsal",
                    "preserve_source_until": time.time() + 14 * 86400,
                    "sqlite": plan["sqlite"], "postgres_major": 18,
                    "session_map": plan.get("session_map", []), "files": {}}
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                relative = str(path.relative_to(stage))
                record = {"sha256": checksum(path), "size": path.stat().st_size}
                if relative in plan["files"]:
                    # Preserve executable intent as authenticated metadata without
                    # making credential-bearing quarantine files executable.
                    record["source_mode"] = safe_path(home, relative).stat().st_mode & 0o777
                manifest["files"][relative] = record
        atomic_json(stage / "manifest.json", manifest)
        encrypt_tree(stage, output, recipient)
    public = {"migration_id": plan["migration_id"], "archive_sha256": checksum(output), "encrypted_bytes": output.stat().st_size,
              "file_count": len(manifest["files"]), "final": final, "preserve_source_until": manifest["preserve_source_until"]}
    if final:
        journal.transition("final_snapshot", public)
    return public


def remap_private_state(root: Path, mapping, runtime_sessions_dir: str):
    """Rewrite only a verified offline copy, never the live source database.

    This schema adapter is intentionally pinned to the inventoried founder schema.
    Operational leases/outbox are cleared, not replayed into a second sender.
    """
    manifest = json.loads((root / "manifest.json").read_text())
    require(manifest.get("scope") == "founder_private", "private_restore_required")
    require(manifest.get("source_release") == SOURCE_RELEASE, "unreviewed_source_version")
    path = root / ".hermes/state.db"
    require(sqlite_inventory(path).get("schema_sha256") == STATE_SCHEMA, "unreviewed_state_schema")
    require(bool(mapping) and all(item.get("kind") == "dm" for item in mapping), "private_import_only_dm")
    require(Path(runtime_sessions_dir).is_absolute(), "absolute_runtime_scope_required")
    require(len({item["old_session_id"] for item in mapping}) == len(mapping), "duplicate_session_map")
    conversations = {}
    for item in mapping:
        conversations.setdefault(item["conversation_id"], []).append(item)
    active_ids = set()
    for items in conversations.values():
        require(len({item["old_chat_jid"] for item in items}) == 1, "cross_identity_history_map")
        active = [item for item in items if item.get("active", len(items) == 1)]
        require(len(active) == 1, "exactly_one_active_session_required")
        active_ids.add(active[0]["old_session_id"])
    mirror = {}
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        ids = [item["old_session_id"] for item in mapping]
        placeholders = ",".join("?" for _ in ids)
        for item in mapping:
            require(SAFE_PART.fullmatch(item["conversation_id"]), "invalid_conversation_id")
            row = db.execute("SELECT * FROM sessions WHERE id=?", (item["old_session_id"],)).fetchone()
            require(row is not None and row["chat_type"] == "dm" and row["source"] == "whatsapp"
                    and row["chat_id"] == item["old_chat_jid"], "session_identity_mismatch")
        # Never transplant in-flight work, leases, or pending sends. They are preserved
        # in the encrypted source snapshot and require individually translated grants.
        for table in ("async_delegations", "compression_locks", "conversation_generations", "delivery_obligations",
                      "gateway_heartbeats", "gateway_hygiene_state", "gateway_routing", "session_turn_leases"):
            db.execute(f'DELETE FROM "{table}"')
        db.execute(f"DELETE FROM messages WHERE session_id NOT IN ({placeholders})", ids)
        db.execute(f"DELETE FROM session_model_usage WHERE session_id NOT IN ({placeholders})", ids)
        db.execute(f"UPDATE sessions SET parent_session_id=NULL WHERE id IN ({placeholders}) AND parent_session_id NOT IN ({placeholders})", ids+ids)
        db.execute(f"DELETE FROM sessions WHERE id NOT IN ({placeholders})", ids)
        db.execute("DELETE FROM system_prompts WHERE hash NOT IN (SELECT system_prompt_hash FROM sessions WHERE system_prompt_hash IS NOT NULL)")
        for item in mapping:
            key = "agent:main:squire:dm:" + item["conversation_id"]
            origin = dict(platform="squire", chat_id=item["conversation_id"], chat_name=None, chat_type="dm",
                          user_id=item.get("actor_id", "owner"), user_name=None, thread_id=None, chat_topic=None)
            timestamp = datetime.now(timezone.utc).isoformat()
            entry = dict(session_key=key, session_id=item["old_session_id"], created_at=timestamp,
                         updated_at=timestamp, display_name=None, platform="squire", chat_type="dm",
                         metadata={}, origin=origin, suspended=False, resume_pending=False,
                         active_turn_token=None, active_turn_started_at=None, transport_profile="default")
            db.execute("""UPDATE sessions SET source='squire',session_key=?,chat_id=?,chat_type='dm',thread_id=NULL,
                origin_json=?,transport_profile='default',handoff_state=NULL,handoff_platform=NULL,handoff_error=NULL
                WHERE id=?""", (key, item["conversation_id"], json.dumps(origin), item["old_session_id"]))
            if item["old_session_id"] in active_ids:
                db.execute("INSERT INTO gateway_routing(scope,session_key,entry_json,updated_at) VALUES (?,?,?,?)",
                           (runtime_sessions_dir, key, json.dumps(entry), time.time()))
                mirror[key] = entry
        require(db.execute("PRAGMA foreign_key_check").fetchall() == [], "import_foreign_key_violation")
        db.commit()
        # Remove deleted group/other-session bytes from the offline copy's free pages.
        db.execute("VACUUM")
    atomic_json(root / ".hermes/sessions/sessions.json", mirror)
    atomic_json(root / "IMPORT_RESULT.json", {"migration_id": manifest["migration_id"], "private_only": True,
        "session_count": len(mapping), "state_sha256": checksum(path), "routing_scope": runtime_sessions_dir,
        "old_pending_work_replayed": False, "requires_scheduler_translation": True})
    return {"private_sessions_remapped": len(mapping), "source_unchanged": True, "runtime_start_authorized": False}


def export_group_history(state: Path, mapping, output: Path, recipient: str):
    """Export only platform-backed visible messages, never model/tool/private context."""
    require(sqlite_inventory(state).get("schema_sha256") == STATE_SCHEMA, "unreviewed_state_schema")
    require(mapping.get("kind") == "group" and SAFE_PART.fullmatch(mapping["conversation_id"]), "group_mapping_required")
    with sqlite3.connect(f"file:{state}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT source,chat_id,chat_type FROM sessions WHERE id=?", (mapping["old_session_id"],)).fetchone()
        require(row is not None and row["source"] == "whatsapp" and row["chat_type"] == "group"
                and row["chat_id"] == mapping["old_chat_jid"], "group_identity_mismatch")
        # The platform ID is necessary evidence that this content belonged to the
        # group's visible conversation; missing IDs are quarantined, not guessed.
        rows = db.execute("""SELECT role,content,timestamp,platform_message_id FROM messages
            WHERE session_id=? AND role IN ('user','assistant') AND platform_message_id IS NOT NULL
            AND platform_message_id!='' AND content IS NOT NULL ORDER BY id""", (mapping["old_session_id"],)).fetchall()
        messages = [dict(row) for row in rows]
    with tempfile.TemporaryDirectory(prefix="squire-group-export-", dir=output.parent) as temporary:
        stage = Path(temporary)
        atomic_json(stage / "visible-history.json", {"conversation_id": mapping["conversation_id"], "messages": messages})
        atomic_json(stage / "manifest.json", {"format": "squire-group-visible-history-v1", "scope": "group",
            "conversation_id": mapping["conversation_id"], "message_count": len(messages),
            "history_sha256": checksum(stage / "visible-history.json"), "contains_private_memory": False,
            "contains_credentials": False, "requires_group_authorization": True})
        encrypt_tree(stage, output, recipient)
    return {"exported_visible_messages": len(messages), "archive_sha256": checksum(output), "scope": "group"}


def decrypt_archive(archive: Path, identity: Path, destination: Path, expected_checksum: str, max_bytes=8 * 1024**3, verifier=None):
    require(checksum(archive) == expected_checksum, "encrypted_archive_checksum_mismatch")
    require(not destination.exists(), "restore_destination_must_not_exist")
    destination.mkdir(parents=True, mode=0o700)
    process = subprocess.Popen(["age", "--decrypt", "--identity", str(identity), str(archive)],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    count, total = 0, 0
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as tar:
            for member in tar:
                count += 1
                total += member.size
                require(count <= 100000 and total <= max_bytes and member.isfile(), "invalid_or_oversized_archive")
                path = safe_path(destination, member.name)
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                require(not path.exists(), "duplicate_archive_member")
                with path.open("xb") as target, tar.extractfile(member) as source:
                    shutil.copyfileobj(source, target)
                path.chmod(0o600)
        require(process.wait(timeout=60) == 0, "archive_authentication_failed")
    except BaseException:
        process.kill()
        process.wait()
        # Leave quarantine for inspection, never promote partially authenticated plaintext.
        atomic_json(destination / "RESTORE_FAILED.json", {"failed": True})
        raise
    return (verifier or verify_tree)(destination)


def verify_tree(root: Path):
    require(not (root / "RESTORE_FAILED.json").exists(), "failed_restore_quarantined")
    manifest = json.loads((root / "manifest.json").read_text())
    require(manifest.get("format") == FORMAT and manifest.get("scope") == "founder_private", "invalid_private_manifest")
    expected = set(manifest["files"]) | {"manifest.json"}
    actual = {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}
    require(actual == expected, "archive_file_set_mismatch")
    for relative, record in manifest["files"].items():
        path = safe_path(root, relative)
        require(path.stat().st_size == record["size"] and checksum(path) == record["sha256"], "archive_member_corrupt")
    for relative, schema in manifest["sqlite"].items():
        path = safe_path(root, relative)
        require(sqlite_inventory(path).get("schema_sha256") == schema, "restored_schema_mismatch")
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            require(db.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "restored_sqlite_corrupt")
    return {"migration_id": manifest["migration_id"], "verified": True, "final": manifest["final"],
            "file_count": len(manifest["files"]), "source_release": manifest["source_release"]}


class Journal:
    def __init__(self, path: Path):
        self.path = path
        self.lock = open(path.with_suffix(path.suffix + ".lock"), "a")
        os.chmod(self.lock.name, 0o600)
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.state = json.loads(path.read_text())

    def close(self):
        self.lock.close()

    @classmethod
    def create(cls, path, migration_id, image_digest):
        require(not path.exists(), "journal_exists")
        require(re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest) is not None, "immutable_target_image_required")
        atomic_json(path, {"migration_id": migration_id, "target_image_digest": image_digest,
                           "stage": "preparing", "gates": {}, "events": [], "created_at": time.time()})
        return cls(path)

    def record_gate(self, name, evidence: Path):
        require(name in GATES | {"target_fenced", "final_restore", "new_writer_healthy"}, "unknown_gate")
        report = json.loads(evidence.read_text())
        require(report.get("passed") is True and report.get("gate") == name
                and report.get("migration_id") == self.state["migration_id"]
                and report.get("target_image_digest") == self.state["target_image_digest"], "gate_evidence_mismatch")
        self.state["gates"][name] = {"sha256": checksum(evidence), "at": time.time()}
        if name == "scheduler_translation":
            require(re.fullmatch(r"[0-9a-f]{64}", report.get("source_crontab_sha256", "")), "reviewed_crontab_hash_required")
            self.state["source_crontab_sha256"] = report["source_crontab_sha256"]
        atomic_json(self.path, self.state)

    def transition(self, stage, details=None):
        allowed = {"preparing": {"fencing"}, "fencing": {"source_fenced", "rolled_back"}, "source_fenced": {"final_snapshot", "rolled_back"},
                   "final_snapshot": {"target_imported", "rolled_back"}, "target_imported": {"active", "rolled_back"},
                   "active": {"rolled_back"}}
        require(stage in allowed.get(self.state["stage"], set()), "invalid_migration_transition")
        if stage in {"fencing", "source_fenced"}:
            require(GATES <= self.state["gates"].keys(), "migration_gates_incomplete")
        if stage == "target_imported":
            require("final_restore" in self.state["gates"], "final_restore_unverified")
        if stage == "active":
            require("new_writer_healthy" in self.state["gates"], "new_writer_unverified")
            self.state["gates"].pop("target_fenced", None)
        if stage == "rolled_back":
            require("target_fenced" in self.state["gates"], "rollback_requires_target_fence")
            require(time.time() - self.state["gates"]["target_fenced"]["at"] <= 300, "target_fence_evidence_stale")
        self.state["events"].append({"from": self.state["stage"], "to": stage, "at": time.time(), "details": details or {}})
        self.state["stage"] = stage
        atomic_json(self.path, self.state)


def fence_source(home: Path, journal: Journal):
    require(journal.state["stage"] in {"preparing", "fencing"} and GATES <= journal.state["gates"].keys(), "migration_gates_incomplete")
    marker = home / ".local/state/squire-migration/source-fenced"
    require("\n" not in str(marker) and " " not in str(marker), "unsafe_source_home")
    original = journal.state.get("original_units", service_inventory())
    journal.state["original_units"] = original
    journal.state["source_marker"] = str(marker)
    journal.state["preserve_source_until"] = time.time() + 14 * 86400
    atomic_json(journal.path, journal.state)
    if journal.state["stage"] == "preparing":
        try:
            current = subprocess.run(["crontab", "-l"], capture_output=True, check=False, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            raise MigrationError("crontab_probe_unavailable") from None
        require(current.returncode in {0, 1}, "crontab_probe_failed")
        cron = current.stdout if current.returncode == 0 else b""
        require(hashlib.sha256(cron).hexdigest() == journal.state.get("source_crontab_sha256"), "source_crontab_changed")
        cron_backup = journal.path.parent / "source-crontab.backup"
        require(not cron_backup.exists(), "crontab_backup_already_exists")
        cron_backup.write_bytes(cron)
        cron_backup.chmod(0o600)
        journal.state["source_crontab_backup"] = str(cron_backup)
        journal.transition("fencing")
    run(["crontab", "-"], stdin=b"# Squire migration: source schedules fenced; original retained privately.\n")
    atomic_json(marker, {"migration_id": journal.state["migration_id"], "at": time.time()})
    # Persistent negative conditions fence watchdog/timer restarts, including reboot.
    # Original unit files and source databases remain untouched for rollback.
    for unit in FENCE_UNITS:
        dropin = home / ".config/systemd/user" / (unit + ".d") / "90-squire-migration-fence.conf"
        require(not dropin.exists() or dropin.read_text() == f"[Unit]\nConditionPathExists=!{marker}\n", "source_already_has_fence_dropin")
        dropin.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        dropin.write_text(f"[Unit]\nConditionPathExists=!{marker}\n")
        dropin.chmod(0o600)
    run(["systemctl", "--user", "daemon-reload"])
    for unit in FENCE_UNITS:
        run(["systemctl", "--user", "stop", unit], timeout=90)
    assert_fenced(home, journal)
    journal.transition("source_fenced", {"units": list(FENCE_UNITS)})


def assert_fenced(home: Path, journal: Journal):
    require(Path(journal.state.get("source_marker", "/nonexistent")).is_file(), "source_fence_marker_missing")
    states = service_inventory()
    require(all(states.get(unit, {}).get("ActiveState") in {"inactive", "failed"} for unit in FENCE_UNITS), "source_writer_still_running")
    # Unknown detached writers cannot be inferred away; the reviewed scheduler gate
    # inventories crontab and non-systemd jobs before this action is enabled.


def rollback_source(home: Path, journal: Journal):
    require("target_fenced" in journal.state["gates"], "rollback_requires_target_fence")
    require(time.time() - journal.state["gates"]["target_fenced"]["at"] <= 300, "target_fence_evidence_stale")
    require(journal.state["stage"] in {"fencing", "source_fenced", "final_snapshot", "target_imported", "active"}, "rollback_wrong_stage")
    for unit in FENCE_UNITS:
        dropin = home / ".config/systemd/user" / (unit + ".d") / "90-squire-migration-fence.conf"
        if dropin.exists():
            require(journal.state["source_marker"] in dropin.read_text(), "fence_dropin_changed")
            dropin.unlink()
    Path(journal.state["source_marker"]).unlink(missing_ok=True)
    run(["systemctl", "--user", "daemon-reload"])
    cron = Path(journal.state["source_crontab_backup"]).read_bytes()
    require(hashlib.sha256(cron).hexdigest() == journal.state["source_crontab_sha256"], "crontab_backup_changed")
    run(["crontab", "-"], stdin=cron)
    for unit in reversed(FENCE_UNITS):
        if journal.state.get("original_units", {}).get(unit, {}).get("ActiveState") == "active":
            run(["systemctl", "--user", "start", unit], timeout=90)
    journal.transition("rolled_back", {"source_data_preserved": True})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--journal", required=True, type=Path)
    init.add_argument("--migration-id", default=None)
    init.add_argument("--image-digest", required=True)
    plan = sub.add_parser("make-plan")
    plan.add_argument("--home", type=Path, default=Path.home())
    plan.add_argument("--postgres-config", type=Path, required=True)
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--migration-id")
    gate = sub.add_parser("record-gate")
    gate.add_argument("--journal", required=True, type=Path)
    gate.add_argument("--gate", required=True)
    gate.add_argument("--evidence", required=True, type=Path)
    snap = sub.add_parser("snapshot")
    snap.add_argument("--home", type=Path, default=Path.home())
    snap.add_argument("--plan", required=True, type=Path)
    snap.add_argument("--output", required=True, type=Path)
    snap.add_argument("--recipient", required=True)
    snap.add_argument("--final", action="store_true")
    snap.add_argument("--journal", type=Path)
    snap.add_argument("--postgres-credentials", choices=["instance_config", "daemon_environment"])
    verify = sub.add_parser("verify")
    verify.add_argument("--root", required=True, type=Path)
    decrypt = sub.add_parser("decrypt")
    decrypt.add_argument("--archive", required=True, type=Path)
    decrypt.add_argument("--identity", required=True, type=Path)
    decrypt.add_argument("--destination", required=True, type=Path)
    decrypt.add_argument("--sha256", required=True)
    remap = sub.add_parser("remap-private")
    remap.add_argument("--root", required=True, type=Path)
    remap.add_argument("--mapping", required=True, type=Path)
    remap.add_argument("--runtime-sessions-dir", required=True)
    remap.add_argument("--execute", action="store_true")
    group = sub.add_parser("export-group")
    group.add_argument("--state", required=True, type=Path)
    group.add_argument("--mapping", required=True, type=Path)
    group.add_argument("--output", required=True, type=Path)
    group.add_argument("--recipient", required=True)
    upload = sub.add_parser("upload")
    upload.add_argument("--archive", required=True, type=Path)
    upload.add_argument("--migration-id", required=True)
    advance = sub.add_parser("advance")
    advance.add_argument("--journal", required=True, type=Path)
    advance.add_argument("--stage", choices=["target_imported", "active"], required=True)
    advance.add_argument("--execute", action="store_true")
    for name in ("fence", "rollback"):
        action = sub.add_parser(name)
        action.add_argument("--home", type=Path, default=Path.home())
        action.add_argument("--journal", required=True, type=Path)
        action.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == "init":
            journal = Journal.create(args.journal, args.migration_id or str(uuid.uuid4()), args.image_digest)
            result = {"migration_id": journal.state["migration_id"], "stage": journal.state["stage"]}
        elif args.command == "record-gate":
            Journal(args.journal).record_gate(args.gate, args.evidence)
            result = {"recorded": args.gate}
        elif args.command == "make-plan":
            plan = make_plan(args.home, json.loads(args.postgres_config.read_text()), migration_id=args.migration_id)
            require(not args.output.exists(), "plan_already_exists")
            atomic_json(args.output, plan)
            result = {"file_count": len(plan["files"]), "sqlite_count": len(plan["sqlite"]),
                      "estimated_plaintext_bytes": plan["estimated_plaintext_bytes"], "symlinks_requiring_review": len(plan["omitted_symlinks"])}
        elif args.command == "snapshot":
            plan = json.loads(args.plan.read_text())
            if args.postgres_credentials:
                plan["postgres"]["credential_source"] = args.postgres_credentials
            result = snapshot(args.home, plan, args.output, args.recipient,
                              final=args.final, journal=Journal(args.journal) if args.journal else None)
        elif args.command == "verify":
            result = verify_tree(args.root)
        elif args.command == "decrypt":
            result = decrypt_archive(args.archive, args.identity, args.destination, args.sha256)
        elif args.command == "remap-private":
            require(args.execute, "explicit_execute_required")
            verify_tree(args.root)
            result = remap_private_state(args.root, json.loads(args.mapping.read_text()), args.runtime_sessions_dir)
        elif args.command == "export-group":
            result = export_group_history(args.state, json.loads(args.mapping.read_text()), args.output, args.recipient)
        elif args.command == "upload":
            result = upload_archive(args.archive, args.migration_id)
        elif args.command == "advance":
            require(args.execute, "explicit_execute_required")
            journal = Journal(args.journal)
            journal.transition(args.stage)
            result = {"stage": journal.state["stage"]}
        else:
            require(args.execute, "explicit_execute_required")
            journal = Journal(args.journal)
            (fence_source if args.command == "fence" else rollback_source)(args.home, journal)
            result = {"stage": journal.state["stage"]}
        print(json.dumps(result, sort_keys=True))
    except MigrationError as exc:
        raise SystemExit("Migration operation failed: " + str(exc)) from None
    except (OSError, ValueError, sqlite3.Error, tarfile.TarError, KeyError):
        # Exceptions can contain paths/argv/SQL values; detailed evidence stays private.
        raise SystemExit("Migration operation failed; no acceptance gate was inferred. Inspect private operator evidence.") from None


if __name__ == "__main__":
    main()
