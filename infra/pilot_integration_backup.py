#!/usr/bin/env python3
"""Private founder integration add-on; never substitutes for the core snapshot."""
import argparse
import json
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from migrate_personal_agent import (FORMAT, SOURCE_RELEASE, MigrationError, atomic_json, backup_sqlite,
    checksum, copy_regular, encrypt_tree, require, safe_path)
from pilot_inventory import sqlite_inventory

ROOTS = (".camofox", ".config", ".codex", ".claude", ".ssh", ".aws", ".netrc", ".gitconfig")
OMIT = {"node_modules", ".git", "cache", "Cache", "Code Cache", "GPUCache", "logs", "__pycache__"}


def plan(home):
    files, databases, omitted, total = [], {}, [], 0
    for name in ROOTS:
        root = home / name
        if not root.exists():
            continue
        if root.is_symlink():
            omitted.append(name)
            continue
        candidates = [root] if root.is_file() else []
        if root.is_dir():
            for current, dirs, names in os.walk(root, followlinks=False):
                kept = []
                for item in dirs:
                    child = Path(current) / item
                    if child.is_symlink() or item in OMIT:
                        omitted.append(str(child.relative_to(home)))
                    else:
                        kept.append(item)
                dirs[:] = kept
                candidates.extend(Path(current) / name for name in names)
                require(len(candidates) <= 100000, "integration_file_limit")
        for item in sorted(candidates):
            relative = str(item.relative_to(home))
            if item.is_symlink() or not item.is_file() or item.name.endswith(("-wal", "-shm", ".sock", ".pid", ".lock")):
                omitted.append(relative)
                continue
            with item.open("rb") as stream:
                is_sqlite = stream.read(16) == b"SQLite format 3\0"
            if is_sqlite:
                schema = sqlite_inventory(item).get("schema_sha256")
                require(schema, "integration_sqlite_unreadable")
                databases[relative] = schema
            else:
                files.append(relative)
            total += item.stat().st_size
    return {"kind": "founder_integration_addon", "roots": list(ROOTS), "files": files, "sqlite": databases,
            "omitted": sorted(omitted), "estimated_plaintext_bytes": total,
            "restore_policy": "private-founder-only; explicit per-integration import; never restore old systemd units or authorized_keys"}


def snapshot(home, output, recipient, migration_id):
    require((home / ".hermes/hermes-agent").resolve().name == SOURCE_RELEASE, "source_release_changed")
    inventory = plan(home)
    require(shutil.disk_usage(output.parent).free > inventory["estimated_plaintext_bytes"] * 2 + 64 * 1024**2,
            "integration_staging_space_insufficient")
    with tempfile.TemporaryDirectory(prefix="squire-integration-", dir=output.parent) as directory:
        stage = Path(directory)
        for relative in inventory["files"]:
            copy_regular(safe_path(home, relative), safe_path(stage, relative))
        for relative, schema in inventory["sqlite"].items():
            backup_sqlite(safe_path(home, relative), safe_path(stage, relative), schema)
        atomic_json(stage / "integration-policy.json", inventory)
        manifest = {"format": FORMAT, "scope": "founder_private", "artifact_kind": "integration_addon",
            "source_release": SOURCE_RELEASE, "migration_id": migration_id, "created_at": time.time(),
            "final": False, "sqlite": inventory["sqlite"], "files": {},
            "consistency": "per_database_online_rehearsal", "preserve_source_until": time.time() + 14 * 86400}
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                relative = str(path.relative_to(stage))
                record = {"size": path.stat().st_size, "sha256": checksum(path)}
                if relative in inventory["files"]:
                    record["source_mode"] = safe_path(home, relative).stat().st_mode & 0o777
                manifest["files"][relative] = record
        atomic_json(stage / "manifest.json", manifest)
        encrypt_tree(stage, output, recipient)
    return {"archive_sha256": checksum(output), "encrypted_bytes": output.stat().st_size,
            "file_count": len(manifest["files"]), "kind": "founder_integration_addon", "final": False}


def source_modes(home, core_plan, output, recipient, migration_id):
    require(core_plan.get("source_release") == SOURCE_RELEASE, "unreviewed_source_version")
    files = sorted(set(core_plan["files"]) | set(plan(home)["files"]))
    modes = {}
    for relative in files:
        path = safe_path(home, relative)
        before = path.stat()
        digest = checksum(path)
        after = path.stat()
        require((before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns), "source_changed_during_mode_inventory")
        modes[relative] = {"sha256": digest, "size": after.st_size, "source_mode": after.st_mode & 0o777}
    with tempfile.TemporaryDirectory(prefix="squire-modes-", dir=output.parent) as directory:
        stage = Path(directory)
        atomic_json(stage / "source-modes.json", {"source_release": SOURCE_RELEASE, "files": modes,
            "policy": "Apply only owner execute bit to approved scripts whose restored checksum matches; credentials remain0600; directories0700"})
        atomic_json(stage / "manifest.json", {"format": FORMAT, "scope": "founder_private", "artifact_kind": "source_modes",
            "source_release": SOURCE_RELEASE, "migration_id": migration_id, "created_at": time.time(), "final": False,
            "sqlite": {}, "files": {"source-modes.json": {"sha256": checksum(stage / "source-modes.json"),
                                                       "size": (stage / "source-modes.json").stat().st_size}}})
        encrypt_tree(stage, output, recipient)
    return {"archive_sha256": checksum(output), "encrypted_bytes": output.stat().st_size, "mode_records": len(modes)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["plan", "snapshot", "source-modes"])
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--recipient")
    parser.add_argument("--migration-id")
    parser.add_argument("--core-plan", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "plan":
            inventory = plan(args.home)
            atomic_json(args.output, inventory)
            print(json.dumps({"files": len(inventory["files"]), "sqlite": len(inventory["sqlite"]),
                              "bytes": inventory["estimated_plaintext_bytes"], "omitted_count": len(inventory["omitted"])}))
        else:
            require(args.recipient and args.migration_id, "recipient_and_migration_id_required")
            if args.command == "source-modes":
                require(args.core_plan, "core_plan_required")
                result = source_modes(args.home, json.loads(args.core_plan.read_text()), args.output, args.recipient, args.migration_id)
            else:
                result = snapshot(args.home, args.output, args.recipient, args.migration_id)
            print(json.dumps(result))
    except MigrationError as exc:
        raise SystemExit(str(exc)) from None
    except Exception:
        raise SystemExit("integration_backup_failed") from None
