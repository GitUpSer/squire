#!/usr/bin/env python3
"""Central daily encrypted control/messaging/transport backups; no receiver key."""
import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

from migrate_personal_agent import MigrationError, atomic_json, checksum, decrypt_archive, encrypt_tree, require, run, safe_path


def s3_client():
    import boto3
    values = {key: os.environ.get("BACKUP_S3_" + key) for key in ("ENDPOINT", "BUCKET", "REGION", "ACCESS_KEY_ID", "SECRET_ACCESS_KEY")}
    require(all(values.values()) and values["ENDPOINT"].startswith("https://"), "backup_credentials_missing")
    return boto3.client("s3", endpoint_url=values["ENDPOINT"], region_name=values["REGION"],
                        aws_access_key_id=values["ACCESS_KEY_ID"], aws_secret_access_key=values["SECRET_ACCESS_KEY"]), values["BUCKET"]


def postgres_dump(url, output):
    parsed = urlsplit(url.replace("postgresql+psycopg://", "postgresql://", 1))
    require(parsed.scheme in {"postgres", "postgresql"} and parsed.hostname and parsed.username, "postgres_url_invalid")
    env = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    env.update(PGHOST=parsed.hostname, PGPORT=str(parsed.port or 5432), PGUSER=unquote(parsed.username),
               PGPASSWORD=unquote(parsed.password or ""), PGDATABASE=unquote(parsed.path.lstrip("/")), PGCONNECT_TIMEOUT="15")
    # Private Railway hostnames stay on its private network; external URL policy belongs
    # to infrastructure provisioning. Connection passwords never enter argv or logs.
    run(["pg_dump", "--no-password", "--format=custom", "--no-owner", "--no-acl", "--file", str(output)], env=env, timeout=1800)
    run(["pg_restore", "--list", str(output)], timeout=60)
    output.chmod(0o600)


def make_database_archive(name, url, output, recipient):
    with tempfile.TemporaryDirectory(prefix="squire-db-stage-") as directory:
        stage = Path(directory)
        stage.chmod(0o700)
        postgres_dump(url, stage / "database.dump")
        atomic_json(stage / "manifest.json", {"format": "squire-ops-backup-v1", "kind": "postgres", "service": name,
            "created_at": time.time(), "files": {"database.dump": {"sha256": checksum(stage / "database.dump"),
            "size": (stage / "database.dump").stat().st_size}}})
        encrypt_tree(stage, output, recipient)


def fetch_transport_archive(url, token, output):
    import httpx
    require(url.startswith(("http://", "https://")) and len(token) >= 32, "transport_backup_config_invalid")
    with httpx.Client(timeout=1800, trust_env=False) as client:
        with client.stream("POST", url.rstrip("/") + "/v1/operator/backup", headers={"Authorization": "Bearer " + token}) as response:
            response.raise_for_status()
            count = 0
            with output.open("xb") as stream:
                output.chmod(0o600)
                for part in response.iter_bytes():
                    count += len(part)
                    require(count <= 8 * 1024**3, "transport_backup_size_limit")
                    stream.write(part)
    with output.open("rb") as stream:
        require(stream.read(21).startswith(b"age-encryption.org/v1"), "transport_backup_not_encrypted")


def upload_and_roundtrip(client, bucket, archive, name, now=None):
    require(re.fullmatch(r"[a-z0-9-]{1,80}", name), "backup_name_invalid")
    now = now or datetime.now(timezone.utc)
    digest = checksum(archive)
    key = f"ops/{name}/{now.strftime('%Y/%m/%d')}/{digest}.age"
    client.upload_file(str(archive), bucket, key, ExtraArgs={"ContentType": "application/octet-stream", "Metadata": {"sha256": digest}})
    with tempfile.TemporaryDirectory(prefix="squire-backup-check-") as directory:
        downloaded = Path(directory) / "roundtrip.age"
        client.download_file(bucket, key, str(downloaded))
        require(checksum(downloaded) == digest, "backup_roundtrip_checksum_mismatch")
    return {"service": name, "object_key": key, "sha256": digest, "encrypted_bytes": archive.stat().st_size,
            "ciphertext_roundtrip_verified": True, "at": now.timestamp()}


def cleanup(client, bucket, prefixes, now=None):
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=30)
    deleted = 0
    for prefix in prefixes:
        require(prefix == "founder/" or re.fullmatch(r"ops/[a-z0-9-]{1,80}/", prefix), "cleanup_prefix_invalid")
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            victims = [{"Key": item["Key"]} for item in page.get("Contents", [])
                       if item["Key"].startswith(prefix) and item["Key"].endswith(".age") and item["LastModified"] < cutoff]
            for offset in range(0, len(victims), 1000):
                batch = victims[offset:offset + 1000]
                response = client.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
                require(not response.get("Errors"), "backup_expiry_failed")
                deleted += len(batch)
    return deleted


def verify_ops_tree(root):
    manifest = json.loads((root / "manifest.json").read_text())
    require(manifest.get("format") == "squire-ops-backup-v1", "ops_manifest_invalid")
    require({str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()} == set(manifest["files"]) | {"manifest.json"}, "ops_file_set_mismatch")
    for name, value in manifest["files"].items():
        path = safe_path(root, name)
        require(path.stat().st_size == value["size"] and checksum(path) == value["sha256"], "ops_file_corrupt")
        if name.endswith(".dump"):
            run(["pg_restore", "--list", str(path)], timeout=60)
        elif name.endswith((".db", ".sqlite3")):
            import sqlite3
            # These are checksum-verified, offline main files. Immutable mode
            # avoids creating WAL/SHM sidecars while validating the archive.
            with contextlib.closing(sqlite3.connect(f"file:{path}?immutable=1", uri=True)) as db:
                require(db.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "ops_sqlite_corrupt")
    return {"verified": True, "service": manifest["service"], "kind": manifest["kind"], "file_count": len(manifest["files"])}


def daily():
    recipient = os.environ.get("BACKUP_AGE_RECIPIENT", "")
    require(recipient.startswith("age1"), "public_backup_recipient_missing")
    client, bucket = s3_client()
    jobs = json.loads(os.environ.get("BACKUP_POSTGRES_DATABASES_JSON", "{}"))
    require(jobs and all(re.fullmatch(r"[A-Z][A-Z0-9_]+", value) for value in jobs.values()), "database_backup_jobs_missing")
    reports, failures = [], []
    with tempfile.TemporaryDirectory(prefix="squire-ops-") as directory:
        root = Path(directory)
        root.chmod(0o700)
        for name, variable in jobs.items():
            try:
                require(re.fullmatch(r"[a-z0-9-]{1,80}", name), "backup_name_invalid")
                archive = root / (name + ".age")
                make_database_archive(name, os.environ[variable], archive, recipient)
                reports.append(upload_and_roundtrip(client, bucket, archive, name))
                archive.unlink()
            except Exception:
                failures.append(name)
        if os.environ.get("BACKUP_TRANSPORT_URL"):
            try:
                archive = root / "transport.age"
                fetch_transport_archive(os.environ["BACKUP_TRANSPORT_URL"], os.environ["BACKUP_TRANSPORT_TOKEN"], archive)
                reports.append(upload_and_roundtrip(client, bucket, archive, "transport"))
            except Exception:
                failures.append("transport")
    # Expire only managed namespaces whose fresh backup passed the download check.
    prefixes = ["ops/" + report["service"] + "/" for report in reports]
    if not failures:
        prefixes.extend(["founder/", "ops/founder-core/", "ops/founder-integrations/"])
    expired = cleanup(client, bucket, prefixes)
    result = {"backups": reports, "failed_services": failures, "expired_archives": expired, "at": time.time()}
    if os.environ.get("BACKUP_STATUS_PATH"):
        atomic_json(Path(os.environ["BACKUP_STATUS_PATH"]), result)
    print(json.dumps(result, sort_keys=True))
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("daily")
    restore = sub.add_parser("restore-verify")
    restore.add_argument("--object-key", required=True)
    restore.add_argument("--sha256", required=True)
    restore.add_argument("--identity", required=True, type=Path)
    restore.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == "daily":
            raise SystemExit(daily())
        require(args.object_key.startswith("ops/") and args.object_key.endswith(".age"), "ops_key_required")
        client, bucket = s3_client()
        with tempfile.TemporaryDirectory(prefix="squire-restore-download-") as directory:
            archive = Path(directory) / "restore.age"
            client.download_file(bucket, args.object_key, str(archive))
            result = decrypt_archive(archive, args.identity, args.destination, args.sha256, verifier=verify_ops_tree)
            print(json.dumps(result))
    except Exception:
        raise SystemExit("backup operation failed; inspect protected status and configured service health") from None


if __name__ == "__main__":
    main()
