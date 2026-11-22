"""Account-store backup runs under the writer mutex and capture pause."""
import asyncio
import shutil
import tempfile
import time
from pathlib import Path


async def snapshot_transport(settings, supervisor, capture):
    from migrate_personal_agent import atomic_json, backup_sqlite, checksum, encrypt_tree, require
    from pilot_inventory import sqlite_inventory
    require(settings.backup_recipient and settings.backup_recipient.startswith("age1"), "backup_recipient_missing")
    root = Path(tempfile.mkdtemp(prefix="transport-backup-"))
    root.chmod(0o700)
    stage = root / "stage"
    stage.mkdir(mode=0o700)
    archive = root / "transport.age"

    def copy_and_encrypt():
        files = {"wacli.db": settings.store_dir / "wacli.db", "session.db": settings.store_dir / "session.db",
                 "transport.sqlite3": settings.data_dir / "transport.sqlite3"}
        require(all(source.is_file() for source in files.values()), "transport_store_not_initialized")
        require(shutil.disk_usage(root).free > sum(source.stat().st_size for source in files.values()) * 2 + 64 * 1024**2,
                "backup_staging_space_insufficient")
        for name, source in files.items():
            require(source.is_file(), "transport_store_not_initialized")
            backup_sqlite(source, stage / name, sqlite_inventory(source)["schema_sha256"])
        require({path.name for path in stage.iterdir()} == set(files), "backup_unexpected_sidecars")
        atomic_json(stage / "manifest.json", {"format": "squire-ops-backup-v1", "kind": "transport",
            "service": "transport", "created_at": time.time(), "provider_account_id": settings.provider_account_id,
            "generation": capture.ledger.get("generation"), "outbound_restore_policy": "disabled_until_operator_fence",
            "files": {name: {"sha256": checksum(stage / name), "size": (stage / name).stat().st_size} for name in files}})
        encrypt_tree(stage, archive, settings.backup_recipient)
        shutil.rmtree(stage)

    try:
        async with supervisor.exclusive(), capture.lock:
            task = asyncio.create_task(asyncio.to_thread(copy_and_encrypt))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # Do not resume the writer or delete staging while the thread is copying.
                await task
                raise
        return root, archive
    except BaseException:
        shutil.rmtree(root)
        raise
