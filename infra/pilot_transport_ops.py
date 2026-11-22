#!/usr/bin/env python3
"""Sanitized transport checks and explicitly invoked capture/backup operations.

Run probe/activate/backup inside the transport container. Tokens stay in its
environment; raw CLI output, session identities and HTTP errors never print.
Restore-layout runs offline on the private operator machine and opens no socket.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.request import Request, ProxyHandler, HTTPRedirectHandler, build_opener


class OperationError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def require(condition, code):
    if not condition:
        raise OperationError(code)


def request(path, *, method="GET", token_name="TRANSPORT_OPERATOR_TOKEN", payload=None, timeout=30):
    token = os.environ.get(token_name, "")
    require(len(token) >= 32, "principal_unconfigured")
    port = int(os.environ.get("PORT", "8080"))
    require(0 < port < 65536, "invalid_port")
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"Authorization": "Bearer " + token}
    if body is not None:
        headers["Content-Type"] = "application/json"
    return build_opener(ProxyHandler({}), NoRedirect()).open(Request(f"http://127.0.0.1:{port}" + path,
        data=body, headers=headers, method=method), timeout=timeout)


def status():
    with request("/v1/status") as response:
        return json.load(response)


def sanitize_status(value):
    result = {key: value.get(key) is True for key in
              ("pairing_mode", "connected", "logged_out", "outbound_enabled", "capture_enabled")}
    result["generation"] = value.get("generation") if type(value.get("generation")) is int else None
    result["capture_error_present"] = bool(value.get("capture_error"))
    result["capture_committed"] = bool(value.get("last_commit"))
    return result


def probe(data_dir=Path("/data"), binary="/usr/local/bin/wacli"):
    result = subprocess.run([binary, "--store", str(data_dir / "wacli"), "--read-only", "--json",
                             "--timeout", "10s", "doctor"], capture_output=True, timeout=15, check=False)
    require(result.returncode == 0, "readonly_doctor_failed")
    envelope = json.loads(result.stdout)
    require(envelope.get("success") is True, "readonly_doctor_failed")
    report = envelope.get("data") or {}
    output = {key: report.get(key) is True for key in ("authenticated", "session_revoked", "lock_held")}
    output["store_error_present"] = bool(report.get("store_error"))
    output["service"] = sanitize_status(status())
    output["configured_outbound_disabled"] = os.environ.get("TRANSPORT_OUTBOUND_ENABLED", "false").lower() == "false"
    sync_count = 0
    for process in Path("/proc").glob("[0-9]*"):
        try:
            if (process / "comm").read_text().strip() == "wacli":
                arguments = (process / "cmdline").read_bytes().split(b"\0")
                sync_count += b"sync" in arguments and b"--follow" in arguments
        except (OSError, ValueError):
            continue
    output["sync_processes"] = sync_count
    return output


def activate():
    value = status()
    require(not value.get("pairing_mode") and value.get("connected") and not value.get("logged_out"), "paired_sync_required")
    require(value.get("outbound_enabled") is False and os.environ.get("TRANSPORT_OUTBOUND_ENABLED", "false").lower() == "false",
            "capture_only_requires_outbound_disabled")
    if value.get("capture_enabled"):
        return {"already_active": True, "service": sanitize_status(value)}
    with request("/v1/operator/activate", method="POST") as response:
        require(json.load(response).get("activated") is True, "activation_not_acknowledged")
    return {"activated": True, "service": sanitize_status(status())}


def backup(output):
    value = status()
    require(not value.get("pairing_mode") and value.get("outbound_enabled") is False, "capture_only_backup_required")
    require(output.parent.is_dir() and not output.exists(), "new_private_output_required")
    descriptor = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    count, digest = 0, hashlib.sha256()
    try:
        with os.fdopen(descriptor, "wb") as target, request("/v1/operator/backup", method="POST",
                token_name="TRANSPORT_BACKUP_TOKEN", timeout=1800) as response:
            header = response.read(22)
            require(header.startswith(b"age-encryption.org/v1\n"), "encrypted_response_required")
            while header:
                count += len(header)
                require(count <= 8 * 1024**3, "backup_size_limit")
                target.write(header)
                digest.update(header)
                header = response.read(1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return {"encrypted_bytes": count, "sha256": digest.hexdigest(), "outbound_enabled": False}


def stage_restore(verified, destination, generation):
    from migrate_personal_agent import offline_sqlite
    from pilot_backup_worker import verify_ops_tree
    verify_ops_tree(verified)
    manifest = json.loads((verified / "manifest.json").read_text())
    require(manifest.get("kind") == "transport" and set(manifest["files"]) == {"wacli.db", "session.db", "transport.sqlite3"},
            "transport_snapshot_required")
    require(type(manifest.get("generation")) is int and generation > manifest["generation"], "new_generation_required")
    require(not destination.exists(), "restore_destination_must_be_new")
    destination.mkdir(mode=0o700)
    try:
        (destination / "wacli").mkdir(mode=0o700)
        for name in manifest["files"]:
            target = destination / name if name == "transport.sqlite3" else destination / "wacli" / name
            shutil.copyfile(verified / name, target)
            target.chmod(0o600)
        with offline_sqlite(destination / "transport.sqlite3") as db:
            row = db.execute("SELECT value FROM metadata WHERE key='generation'").fetchone()
            require(row is not None and json.loads(row[0]) == manifest["generation"]
                    and type(generation) is int and generation > 0, "ledger_generation_mismatch")
            db.execute("BEGIN IMMEDIATE")
            for key, value in (("outbound_enabled", False), ("generation", generation)):
                db.execute("INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (key, json.dumps(value)))
            db.execute("UPDATE sends SET state='uncertain',error_code='restored_dispatch_intent' WHERE state='dispatching'")
    except BaseException:
        # Keep failed material visibly quarantined; never overwrite an existing store.
        (destination / "RESTORE_FAILED").write_text("restore preparation failed\n")
        raise
    return {"offline_layout_prepared": True, "generation": generation, "outbound_enabled": False,
            "requires_old_writer_fence": True, "requires_remote_checkpoint_reconciliation": True}


def restore_layout(archive, identity, checksum, destination, generation):
    from migrate_personal_agent import decrypt_archive
    from pilot_backup_worker import verify_ops_tree
    require(destination.parent.is_dir() and not destination.exists(), "restore_destination_must_be_new")
    with tempfile.TemporaryDirectory(prefix="transport-restore-", dir=destination.parent) as directory:
        verified = Path(directory) / "verified"
        decrypt_archive(archive, identity, verified, checksum, verifier=verify_ops_tree)
        return stage_restore(verified, destination, generation)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("probe")
    check.add_argument("--data-dir", type=Path, default=Path("/data"))
    check.add_argument("--binary", default="/usr/local/bin/wacli")
    sub.add_parser("activate").add_argument("--execute", action="store_true", required=True)
    save = sub.add_parser("backup")
    save.add_argument("--execute", action="store_true", required=True)
    save.add_argument("--output", type=Path, required=True)
    restore = sub.add_parser("restore-layout")
    restore.add_argument("--archive", type=Path, required=True)
    restore.add_argument("--identity", type=Path, required=True)
    restore.add_argument("--sha256", required=True)
    restore.add_argument("--destination", type=Path, required=True)
    restore.add_argument("--generation", type=int, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == "probe":
            result = probe(args.data_dir, args.binary)
        elif args.command == "activate":
            result = activate()
        elif args.command == "backup":
            result = backup(args.output)
        else:
            result = restore_layout(args.archive, args.identity, args.sha256, args.destination, args.generation)
        print(json.dumps(result, sort_keys=True))
    except OperationError as exc:
        raise SystemExit(str(exc)) from None
    except Exception:
        raise SystemExit("transport_operation_failed_no_private_details_logged") from None


if __name__ == "__main__":
    main()
