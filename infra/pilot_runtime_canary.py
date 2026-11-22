#!/usr/bin/env python3
"""Read-only, counts-only probe for an isolated pilot image canary.

Run inside the image with /opt/hermes/.venv/bin/python. This does not register
a runtime, enqueue an event, send a message, invoke a model, or modify memory.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
from urllib.request import urlopen


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def health(port):
    try:
        with urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as response:
            return response.status == 200
    except Exception:
        return False


def versions(python, names):
    code = "import json,importlib.metadata as m;print(json.dumps({n:m.version(n) for n in " + repr(names) + "}))"
    result = subprocess.run([python, "-c", code], capture_output=True, text=True, timeout=30)
    return json.loads(result.stdout) if result.returncode == 0 else {}


def probe(expected_image, variant, require_adapter=True, allow_activity=False):
    import yaml
    home = Path(os.environ.get("HERMES_HOME", "/opt/data"))
    runtime = os.environ.get("SQUIRE_RUNTIME_ID", "")
    failures = []
    def check(name, value):
        if not value:
            failures.append(name)
        return bool(value)

    config = yaml.safe_load((home / "config.yaml").read_text())
    profile = json.loads((home / ".squire/runtime-profile.json").read_text())
    proc = subprocess.run(["/opt/squire/venv/bin/supervisorctl", "-c", "/opt/squire/supervisord.conf", "status"],
                          capture_output=True, text=True, timeout=15)
    processes = {parts[0]: parts[1] for line in proc.stdout.splitlines() if len(parts := line.split()) >= 2}
    api_versions = versions("/opt/squire/hindsight-venv/bin/python", ["hindsight-api-slim", "pg0-embedded", "sqlalchemy"])
    client_versions = versions("/opt/hermes/.venv/bin/python", ["hindsight-client"])
    expected_api, expected_pg = {"standard": ("0.9.0", "0.15.1"), "founder": ("0.5.2", "0.12.3")}[variant]
    row, queue_counts = None, {}
    database = home / ".squire/messaging.sqlite3"
    if database.exists():
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            row = db.execute("SELECT pid,generation,ready,model_ready,updated FROM runtime WHERE id=1").fetchone()
            queue_counts = {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("inbox", "outbox", "tools")}
    adapter_ready = bool(row and row[2] and row[1] == int(os.environ.get("SQUIRE_DEPLOYMENT_GENERATION", "0"))
                         and time.time() - row[4] < 30 and Path(f"/proc/{row[0]}").exists())
    sealed_paths = {}
    for name in (".env", "auth.json"):
        path = home / name
        sealed_paths[name] = ((not path.exists() and not path.is_symlink())
                              or (path.is_symlink() and path.exists() and path.resolve().is_relative_to(Path("/dev/shm"))))
    mount_points = {line.split()[4] for line in Path("/proc/self/mountinfo").read_text().splitlines()}
    gateway_process_count = 0
    for path in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            arguments = path.read_bytes().split(b"\0")
        except OSError:
            continue
        gateway_process_count += any(arguments[index:index + 2] == [b"gateway", b"run"]
                                     for index in range(len(arguments) - 1))
    report = {
        "format": "squire-runtime-canary-v1", "runtime_id_sha256": digest(runtime), "variant": variant,
        "image_matches": check("image_identity", os.environ.get("SQUIRE_IMAGE_REF") == expected_image),
        "variant_matches": check("image_variant", os.environ.get("SQUIRE_HINDSIGHT_VARIANT") == variant),
        "volume_layout_matches": check("volume_layout", str(home) == os.environ.get("HOME") == os.environ.get("SQUIRE_VOLUME") == "/opt/data"),
        "volume_mounted": check("persistent_volume", str(home.resolve()) in mount_points),
        "initialized": check("initialized", (home / ".squire/initialized").exists()),
        "scope_matches": check("scope_identity", bool(runtime) and profile.get("runtime_id") == runtime
                               and profile.get("conversation_id") == os.environ.get("SQUIRE_CONVERSATION_ID")
                               and profile.get("kind") == os.environ.get("SQUIRE_RUNTIME_KIND") == "dm"),
        "only_squire_platform": check("transport_isolation", set(config.get("platforms", {})) == {"squire"}),
        "web_health": check("web_health", health(int(os.environ.get("PORT", "8080")))),
        "memory_health": check("memory_health", health(9177)),
        "browser_health": check("browser_health", health(9377)),
        "memory_versions_match": check("memory_versions", api_versions == {"hindsight-api-slim": expected_api, "pg0-embedded": expected_pg, "sqlalchemy": "2.0.52"}
                                       and client_versions == {"hindsight-client": "0.10.3"}),
        "memory_versions": api_versions, "client_versions": client_versions, "processes": processes,
        "core_processes_running": check("core_processes", all(processes.get(name) == "RUNNING" for name in
                                            ("hindsight", "webhook", "secrets-sync", "camofox"))),
        "adapter_ready": adapter_ready,
        "gateway_process_count": gateway_process_count,
        "model_configured": bool(row and row[3]),
        "queue_counts": queue_counts,
        "sealed_paths": sealed_paths,
        "secrets_on_tmpfs": check("secret_paths", all(sealed_paths.values())),
        "no_runtime_activity": not any(queue_counts.values()),
        "memory_worker_disabled": check("memory_worker_fence", os.environ.get("HINDSIGHT_API_WORKER_ENABLED") == "false"),
    }
    if require_adapter:
        check("adapter_ready", adapter_ready)
        check("gateway_running", processes.get("gateway") == "RUNNING")
        check("single_gateway_owner", gateway_process_count == 1)
    else:
        check("gateway_fenced", gateway_process_count == 0)
    if not allow_activity:
        check("unexpected_runtime_activity", not any(queue_counts.values()))
    report.update(passed=not failures, failures=failures, live_delivery_verified=False, model_request_verified=False)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-image", required=True)
    parser.add_argument("--variant", choices=("standard", "founder"), required=True)
    parser.add_argument("--gateway-fenced", action="store_true")
    parser.add_argument("--allow-activity", action="store_true")
    args = parser.parse_args()
    try:
        report = probe(args.expected_image, args.variant, not args.gateway_fenced, args.allow_activity)
    except Exception as exc:
        print(json.dumps({"passed": False, "probe_error_type": type(exc).__name__}))
        raise SystemExit(1) from None
    print(json.dumps(report, sort_keys=True))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
