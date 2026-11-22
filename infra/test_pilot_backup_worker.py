import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import pilot_backup_worker as worker
from migrate_personal_agent import MigrationError


class Storage:
    def __init__(self):
        self.objects = {}
        self.deleted = []
        self.corrupt = False

    def upload_file(self, filename, bucket, key, ExtraArgs):
        self.objects[key] = Path(filename).read_bytes()

    def download_file(self, bucket, key, filename):
        Path(filename).write_bytes(b"corrupt" if self.corrupt else self.objects[key])

    def get_paginator(self, name):
        return self

    def paginate(self, Bucket, Prefix):
        return [{"Contents": self.listing}]

    def delete_objects(self, Bucket, Delete):
        self.deleted.extend(item["Key"] for item in Delete["Objects"])
        return {}


def test_upload_requires_real_download_checksum(tmp_path):
    archive = tmp_path / "test.age"
    archive.write_bytes(b"age-encryption.org/v1\nencrypted-test")
    client = Storage()
    result = worker.upload_and_roundtrip(client, "bucket", archive, "control-db")
    assert result["ciphertext_roundtrip_verified"]
    assert result["object_key"].startswith("ops/control-db/")
    client.corrupt = True
    with pytest.raises(MigrationError, match="roundtrip_checksum_mismatch"):
        worker.upload_and_roundtrip(client, "bucket", archive, "control-db")


def test_cleanup_exact_age_and_managed_scope_only():
    client = Storage()
    now = datetime.now(timezone.utc)
    client.listing = [
        {"Key": "ops/control-db/expired.age", "LastModified": now - timedelta(days=31)},
        {"Key": "ops/control-db/recent.age", "LastModified": now - timedelta(days=29)},
        {"Key": "ops/control-db/keep.txt", "LastModified": now - timedelta(days=90)},
        {"Key": "runtimes/other-private-repository/expired.age", "LastModified": now - timedelta(days=90)},
    ]
    assert worker.cleanup(client, "bucket", ["ops/control-db/"], now) == 1
    assert client.deleted == ["ops/control-db/expired.age"]
    with pytest.raises(MigrationError, match="prefix_invalid"):
        worker.cleanup(client, "bucket", ["runtimes/"])


def test_postgres_secret_uses_environment_not_argv(tmp_path, monkeypatch):
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[0] == "pg_dump":
            Path(argv[-1]).write_bytes(b"fake dump")
        return b""
    monkeypatch.setattr(worker, "run", run)
    worker.postgres_dump("postgresql://dbuser:secret%40sentinel@postgres.internal:5432/control", tmp_path / "database.dump")
    assert calls[0][1]["env"]["PGPASSWORD"] == "secret@sentinel"
    assert "secret@sentinel" not in " ".join(calls[0][0])


def test_founder_uploaded_namespaces_expire_only_after_success(monkeypatch):
    monkeypatch.setenv("BACKUP_AGE_RECIPIENT", "age1synthetic")
    monkeypatch.setenv("BACKUP_POSTGRES_DATABASES_JSON", '{"control":"CONTROL_DATABASE_URL"}')
    monkeypatch.setenv("CONTROL_DATABASE_URL", "synthetic")
    monkeypatch.delenv("BACKUP_TRANSPORT_URL", raising=False)
    monkeypatch.setattr(worker, "s3_client", lambda: (object(), "bucket"))
    def archive(name, url, output, recipient):
        output.write_bytes(b"ciphertext")
    monkeypatch.setattr(worker, "make_database_archive", archive)
    monkeypatch.setattr(worker, "upload_and_roundtrip", lambda *args: {"service": "control"})
    cleaned = []
    monkeypatch.setattr(worker, "cleanup", lambda client, bucket, prefixes: cleaned.extend(prefixes) or 0)
    assert worker.daily() == 0
    assert "ops/founder-core/" in cleaned and "ops/founder-integrations/" in cleaned
    cleaned.clear()
    def failed(*args):
        raise RuntimeError("failed")
    monkeypatch.setattr(worker, "make_database_archive", failed)
    assert worker.daily() == 1
    assert not cleaned
