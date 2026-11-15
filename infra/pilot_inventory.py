#!/usr/bin/env python3
"""Bounded, read-only founder inventory. Never emits content or credential values."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

UNITS = ("hermes-gateway.service", "hermes-stall-recovery.service", "hindsight-daemon.service",
         "hindsight-healthcheck.timer", "camofox-server.service")
ROOTS = (".hermes", ".hindsight", ".pg0/instances/hindsight-embed-hermes")
EXCLUDED = {".git", "node_modules", ".venv", "venv", "cache", ".cache", "releases", "logs"}


def bounded_files(root: Path, depth=4, maximum=10000):
    count = 0
    if not root.is_dir():
        return
    for current, dirs, files in os.walk(root, followlinks=False):
        relative = Path(current).relative_to(root)
        dirs[:] = [name for name in dirs if name not in EXCLUDED and not (Path(current) / name).is_symlink()]
        if len(relative.parts) >= depth:
            dirs[:] = []
        for name in sorted(files):
            item = Path(current) / name
            if item.is_symlink():
                continue
            count += 1
            if count > maximum:
                return
            yield item


def sqlite_inventory(path: Path):
    result = {"size": path.stat().st_size, "tables": []}
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1) as db:
            db.execute("PRAGMA query_only=ON")
            schema = db.execute("SELECT name,type,sql FROM sqlite_master WHERE type IN ('table','index','trigger') ORDER BY name").fetchall()
            result["schema_sha256"] = hashlib.sha256(json.dumps(schema).encode()).hexdigest()
            for name, kind, _ in schema:
                if kind != "table" or name.startswith("sqlite_"):
                    continue
                quoted = '"' + name.replace('"', '""') + '"'
                columns = [{"name": row[1], "type": row[2], "primary_key": bool(row[5])}
                           for row in db.execute(f"PRAGMA table_info({quoted})")]
                result["tables"].append({"name": name, "columns": columns})
    except sqlite3.Error:
        result["error"] = "sqlite_inspection_failed"
    return result


def service_inventory():
    result = {}
    for unit in UNITS:
        try:
            run = subprocess.run(["systemctl", "--user", "show", unit, "--property=ActiveState,SubState,MainPID,FragmentPath"],
                                 text=True, capture_output=True, timeout=5, check=False)
            values = dict(line.split("=", 1) for line in run.stdout.splitlines() if "=" in line)
            result[unit] = {key: values.get(key) for key in ("ActiveState", "SubState", "MainPID", "FragmentPath")}
        except (OSError, subprocess.TimeoutExpired):
            result[unit] = {"error": "service_probe_unavailable"}
    return result


def postgres_inventory(home: Path):
    """postmaster.pid is a hint; require a matching live process before trusting it."""
    instance_root = home / ".pg0/instances"
    instances = []
    for marker in sorted(instance_root.glob("*/data/postmaster.pid")) if instance_root.exists() else []:
        try:
            lines = marker.read_text().splitlines()
            pid = int(lines[0])
            proc = Path("/proc") / str(pid)
            executable = (proc / "exe").resolve()
            args = (proc / "cmdline").read_bytes().split(b"\0")
            claimed = Path(lines[1]).resolve()
            matches = executable.name == "postgres" and str(claimed).encode() in args and claimed == marker.parent.resolve()
            instances.append({"data_dir": str(claimed), "pid": pid, "port": int(lines[3]),
                              "socket_dir": lines[4], "live_process_matches": matches,
                              "server_executable": str(executable) if matches else None,
                              "major_version": (marker.parent / "PG_VERSION").read_text().strip()})
        except (OSError, ValueError, IndexError):
            instances.append({"data_dir": str(marker.parent), "error": "unverified_postmaster"})
    return instances


def inventory(home: Path):
    home = home.resolve()
    result = {"format": "squire-founder-inventory-v1", "home": str(home), "roots": {},
              "sqlite": {}, "credential_key_names": [], "release_links": {},
              "free_bytes": shutil.disk_usage(home).free, "units": service_inventory(),
              "postgres": postgres_inventory(home),
              "tools": {name: bool(shutil.which(name)) for name in ("age", "tar", "systemctl", "python3")}}
    try:
        cron = subprocess.run(["crontab", "-l"], capture_output=True, check=False, timeout=5)
        result["crontab"] = {"sha256": hashlib.sha256(cron.stdout if cron.returncode == 0 else b"").hexdigest(),
                             "present": cron.returncode == 0, "entry_count": sum(1 for line in cron.stdout.splitlines() if line.strip() and not line.lstrip().startswith(b"#"))}
    except (OSError, subprocess.TimeoutExpired):
        result["crontab"] = {"error": "probe_unavailable"}
    names = set()
    for root_name in ROOTS:
        root = home / root_name
        files = list(bounded_files(root))
        if root_name == ".hermes":
            # Large session trees must not hide the scheduler or top-level stores.
            priority = [path for path in root.iterdir() if path.is_file() and not path.is_symlink()]
            priority += list(bounded_files(root / "cron", depth=3, maximum=1000))
            files = list(dict.fromkeys([*priority, *files]))
        result["roots"][root_name] = {"present": root.exists(), "bounded_file_count": len(files),
                                    "bounded_bytes": sum(item.stat().st_size for item in files),
                                    "depth_limit": 4, "file_limit": 10000}
        for item in files:
            if item.suffix in {".db", ".sqlite", ".sqlite3"} and item.stat().st_size > 0:
                # Only database relative paths are surfaced; never arbitrary session filenames.
                result["sqlite"][str(item.relative_to(home))] = sqlite_inventory(item)
            if item.name in {".env", "env", "credentials.env"} and item.stat().st_size < 1024 * 1024:
                for line in item.read_text(errors="replace").splitlines():
                    match = re.match(r"(?:export\s+)?([A-Z][A-Z0-9_]{1,100})\s*=", line.strip())
                    if match:
                        names.add(match.group(1))
    for candidate in (home / ".hermes").iterdir() if (home / ".hermes").exists() else []:
        if candidate.is_symlink():
            target = candidate.resolve()
            if ".hermes/releases" in str(target):
                result["release_links"][candidate.name] = str(target)
    result["credential_key_names"] = sorted(names)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    encoded = json.dumps(inventory(args.home), indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(encoded)
    else:
        print(encoded, end="")


if __name__ == "__main__":
    main()
