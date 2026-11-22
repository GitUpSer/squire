import pytest

import pilot_hindsight_upgrade as upgrade
import pilot_restore_drill as drill


def test_source_baseline_rejects_newer_migration_before_connect(tmp_path, monkeypatch):
    monkeypatch.setattr(upgrade, "connection", lambda *args: pytest.fail("must not connect"))
    with pytest.raises(drill.DrillError, match="matching_version"):
        upgrade.upgrade(tmp_path / "wheel", tmp_path / "report", "0.9.0", "hindsight-source")


def test_unverified_wheel_never_runs_database_inventory(tmp_path, monkeypatch):
    wheel = tmp_path / "unverified.whl"
    wheel.write_bytes(b"not-the-pinned-wheel")
    monkeypatch.setenv("RAILWAY_SERVICE_ID", drill.RESTORE_SERVICE_ID)
    monkeypatch.setattr(upgrade, "inventory", lambda *args: pytest.fail("must not inspect or migrate"))
    with pytest.raises(drill.DrillError, match="unverified_hindsight_wheel"):
        upgrade.upgrade(wheel, tmp_path / "report")


def test_database_credentials_remain_local_to_isolated_service(monkeypatch):
    monkeypatch.setenv("RAILWAY_SERVICE_ID", drill.RESTORE_SERVICE_ID)
    monkeypatch.setenv("PGHOST", "production.example")
    monkeypatch.setenv("PGDATABASE", "production")
    monkeypatch.setenv("SQUIRE_DRILL_PG_PORT", "55432")
    environment = drill.connection(drill.DATABASES["hindsight"])
    assert environment["PGHOST"] == "127.0.0.1"
    assert environment["PGDATABASE"] == "squire_drill_hindsight"
    assert environment["PGPORT"] == "55432"
