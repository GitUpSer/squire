"""Initialize only the volume root, then permanently drop root before service code."""
import os
import stat
from pathlib import Path


def prepare_volume(path=Path("/data"), uid=10001, gid=10001, require_mount=True):
    if path != Path("/data"):
        raise RuntimeError("container volume must be /data")
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError("invalid data volume")
    if require_mount and not os.path.ismount(path):
        raise RuntimeError("persistent volume required")
    # A Railway mount hides the image layer's ownership. Only its root needs
    # initialization; all application files are subsequently created as uid10001.
    # Existing/imported content must already belong to that uid, never recursively
    # change arbitrary mounted paths or follow symlinks.
    for name in ("wacli", "transport.sqlite3", "transport.lock"):
        item = path / name
        if item.is_symlink():
            raise RuntimeError("unsafe volume entry")
        if item.exists() and item.stat().st_uid != uid:
            raise RuntimeError("imported data requires explicit ownership repair")
    os.chown(path, uid, gid, follow_symlinks=False)
    os.chmod(path, 0o700, follow_symlinks=False)


def main():
    os.umask(0o077)
    if os.geteuid() == 0:
        if os.environ.get("TRANSPORT_DATA_DIR", "/data") != "/data":
            raise SystemExit("container volume must be /data")
        prepare_volume(require_mount=os.environ.get("TRANSPORT_REQUIRE_MOUNT", "true").lower() == "true")
        os.setgroups([])
        os.setgid(10001)
        os.setuid(10001)
    os.execvp("squire-whatsapp-transport", ["squire-whatsapp-transport"])


if __name__ == "__main__":
    main()
