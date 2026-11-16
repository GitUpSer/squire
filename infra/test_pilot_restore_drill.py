import hashlib

import pytest

import pilot_restore_drill as drill


def test_wrong_service_cannot_run_sql(tmp_path, monkeypatch):
    archive = tmp_path / "db.dump"
    archive.write_bytes(b"synthetic")
    monkeypatch.setenv("RAILWAY_SERVICE_ID", "production-database")
    monkeypatch.setattr(drill, "run", lambda *args, **kwargs: pytest.fail("must not execute a command"))
    with pytest.raises(drill.DrillError, match="not_the_isolated"):
        drill.restore("control", archive, hashlib.sha256(b"synthetic").hexdigest())


def test_checksum_failure_cannot_create_database(tmp_path, monkeypatch):
    archive = tmp_path / "db.dump"
    archive.write_bytes(b"synthetic")
    monkeypatch.setenv("RAILWAY_SERVICE_ID", drill.RESTORE_SERVICE_ID)
    monkeypatch.setattr(drill, "run", lambda *args, **kwargs: pytest.fail("must not execute a command"))
    with pytest.raises(drill.DrillError, match="dump_checksum"):
        drill.restore("control", archive, "0"*64)


def test_existing_drill_database_is_never_overwritten(tmp_path, monkeypatch):
    archive = tmp_path / "db.dump"
    archive.write_bytes(b"synthetic")
    monkeypatch.setenv("RAILWAY_SERVICE_ID", drill.RESTORE_SERVICE_ID)
    queries = []
    def sql(statement, env):
        queries.append(statement)
        return "180001" if statement == "SHOW server_version_num" else "1"
    monkeypatch.setattr(drill, "sql", sql)
    monkeypatch.setattr(drill, "run", lambda *args, **kwargs: pytest.fail("must not restore"))
    with pytest.raises(drill.DrillError, match="already_exists"):
        drill.restore("control", archive, hashlib.sha256(b"synthetic").hexdigest())
    assert not any("CREATE" in q or "DROP" in q for q in queries)
