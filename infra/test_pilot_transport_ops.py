import json
import sqlite3
import subprocess

import pytest

import pilot_transport_ops as ops
from migrate_personal_agent import checksum
from migrate_personal_agent import offline_sqlite


def test_probe_never_discloses_account_or_raw_errors(tmp_path, monkeypatch):
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps({"success": True, "data": {
            "authenticated": True, "linked_jid": "private-jid", "lock_info": "private-path",
            "store_error": "secret", "session_revoked": False}}).encode(), b"private warning")
    monkeypatch.setattr(ops.subprocess, "run", run)
    monkeypatch.setattr(ops, "status", lambda: {"provider_account_id": "private-account", "capture_error": "private-content",
        "generation": 1, "outbound_enabled": False, "pairing_mode": True})
    result = ops.probe(tmp_path, "wacli")
    assert result["authenticated"] and result["store_error_present"]
    assert "private" not in json.dumps(result) and "secret" not in json.dumps(result)
    assert "--read-only" in calls[0] and "--connect" not in calls[0]


def test_activate_refuses_enabled_sender(monkeypatch):
    monkeypatch.setattr(ops, "status", lambda: {"connected": True, "outbound_enabled": True})
    monkeypatch.setattr(ops, "request", lambda *args, **kwargs: pytest.fail("must not mutate"))
    with pytest.raises(ops.OperationError, match="outbound_disabled"):
        ops.activate()


def test_operator_requests_do_not_follow_redirects():
    assert ops.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://other.invalid/") is None


def test_restore_preserves_uncertain_and_disables_outbound(tmp_path):
    source = tmp_path / "verified"
    source.mkdir()
    for name in ("session.db", "wacli.db"):
        with sqlite3.connect(source / name) as db:
            db.execute("CREATE TABLE fixture(id)")
    with offline_sqlite(source / "transport.sqlite3") as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)")
        db.execute("INSERT INTO metadata VALUES('outbound_enabled','true')")
        db.execute("INSERT INTO metadata VALUES('generation','1')")
        db.execute("CREATE TABLE sends(state TEXT,error_code TEXT)")
        db.execute("INSERT INTO sends VALUES('dispatching',NULL)")
        db.execute("INSERT INTO sends VALUES('uncertain','original')")
    manifest = {"format": "squire-ops-backup-v1", "kind": "transport", "service": "transport", "generation": 1,
        "files": {p.name: {"sha256": checksum(p), "size": p.stat().st_size} for p in source.iterdir()}}
    (source / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ops.OperationError, match="new_generation"):
        ops.stage_restore(source, tmp_path / "target", 1)
    result = ops.stage_restore(source, tmp_path / "target", 2)
    assert not result["outbound_enabled"]
    with sqlite3.connect(f"file:{tmp_path / 'target/transport.sqlite3'}?immutable=1", uri=True) as db:
        assert dict(db.execute("SELECT key,value FROM metadata")) == {"outbound_enabled": "false", "generation": "2"}
        assert db.execute("SELECT state,error_code FROM sends").fetchall() == [
            ("uncertain", "restored_dispatch_intent"), ("uncertain", "original")]
    assert (tmp_path / "target/wacli/session.db").stat().st_mode & 0o777 == 0o600
    for name, item in manifest["files"].items():
        assert checksum(source / name) == item["sha256"]
    for name in ("session.db", "wacli.db"):
        assert checksum(tmp_path / "target/wacli" / name) == manifest["files"][name]["sha256"]
    manifest["generation"] = 0
    (source / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ops.OperationError, match="ledger_generation_mismatch"):
        ops.stage_restore(source, tmp_path / "mismatched-generation", 1)
