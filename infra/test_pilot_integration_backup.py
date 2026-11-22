import sqlite3
from pathlib import Path

import pilot_integration_backup as addon


def test_addon_inventory_excludes_links_cache_and_uses_sqlite_backup(tmp_path):
    home = tmp_path / "home"
    config = home / ".config"
    config.mkdir(parents=True)
    (config / "auth.json").write_text('{"token":"synthetic-secret"}')
    (config / "outside").symlink_to(tmp_path / "elsewhere")
    cache = config / "cache"
    cache.mkdir()
    (cache / "cache-secret").write_text("omit")
    with sqlite3.connect(config / "state.db") as db:
        db.execute("CREATE TABLE state(value)")
        db.execute("INSERT INTO state VALUES ('synthetic')")
    result = addon.plan(home)
    assert result["files"] == [".config/auth.json"]
    assert list(result["sqlite"]) == [".config/state.db"]
    assert ".config/outside" in result["omitted"] and ".config/cache" in result["omitted"]
    assert "synthetic-secret" not in str(result)


def test_addon_snapshot_has_separate_explicit_import_policy(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    (home / ".hermes/hermes-agent").symlink_to(home / addon.SOURCE_RELEASE)
    (home / ".camofox").mkdir()
    (home / ".camofox/auth.json").write_text("synthetic")
    seen = {}
    def encrypt(stage, output, recipient):
        import json
        seen.update(json.loads((stage / "manifest.json").read_text()))
        output.write_bytes(b"encrypted-test")
    monkeypatch.setattr(addon, "encrypt_tree", encrypt)
    result = addon.snapshot(home, tmp_path / "addon.age", "age1synthetic", "test-addon")
    assert result["kind"] == "founder_integration_addon"
    assert seen["artifact_kind"] == "integration_addon" and not seen["final"]
    assert "integration-policy.json" in seen["files"]
    assert ".hermes/state.db" not in seen["files"]
