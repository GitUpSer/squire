#!/usr/bin/env python3
"""Verify an explicitly reviewed founder home before its first Squire boot."""
import hashlib
import json
import os
from pathlib import Path
import re


REQUIRED = {"config.yaml", "SOUL.md", "hindsight/config.json", "plugins/hindsight/__init__.py"}


def verify(home, state, env):
    if (env.get("SQUIRE_RUNTIME_KIND") != "dm"
            or env.get("SQUIRE_HINDSIGHT_VARIANT") != "founder"
            or env.get("HERMES_SKIP_CONFIG_MIGRATION") != "1"):
        raise ValueError("restored home requires a founder DM and reviewed config migration")
    marker = state / "restored-home.json"
    if marker.is_symlink() or not marker.is_file():
        raise ValueError("reviewed restored-home manifest missing")
    data = json.loads(marker.read_text())
    image = env.get("SQUIRE_IMAGE_REF", "")
    if (data.get("format") != "squire-restored-home-v1"
            or not env.get("TENANT_ID") or data.get("runtime_id") != env["TENANT_ID"]
            or not re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", image)
            or data.get("image_ref") != image or not data.get("migration_id")):
        raise ValueError("restored-home identity mismatch")
    files = data.get("files")
    if not isinstance(files, dict) or not REQUIRED <= files.keys():
        raise ValueError("restored-home critical files missing")
    for name, digest in files.items():
        relative = Path(name)
        if relative.is_absolute() or not relative.parts or any(p in {"..", "."} for p in relative.parts):
            raise ValueError("invalid restored-home path")
        path = home
        for part in relative.parts:
            path = path / part
            if path.is_symlink():
                raise ValueError("restored-home symlink is not allowed")
        if not path.is_file() or not re.fullmatch(r"[0-9a-f]{64}", str(digest)):
            raise ValueError("restored-home file unavailable")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise ValueError("restored-home file changed after review")


if __name__ == "__main__":
    try:
        home = Path(os.environ["HERMES_HOME"])
        verify(home, Path(os.environ.get("SQUIRE_STATE_DIR", str(home / ".squire"))), os.environ)
    except (ValueError, KeyError, OSError, TypeError):
        raise SystemExit("Restored-home verification failed; refusing to seed or start services") from None
    print("Reviewed restored-home identity and files verified")
