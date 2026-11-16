#!/usr/bin/env python3
"""Recover reviewed legacy DM routing from exact Hindsight metadata, offline only.

This never infers a chat from an actor, message text, display name or timestamp.
The output is a new database for the existing private-history remapper, not a
runtime-ready database. Originals and ambiguous sessions remain unchanged.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile

from migrate_personal_agent import MigrationError, checksum, require


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def copy_value(value):
    if value == r"\N":
        return None
    def decode(match):
        token = match[1]
        if re.fullmatch(r"[0-7]{1,3}", token):
            return chr(int(token, 8))
        if re.fullmatch(r"x[0-9a-fA-F]{1,2}", token):
            return chr(int(token[1:], 16))
        return {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}.get(token, token)
    return re.sub(r"\\([0-7]{1,3}|x[0-9a-fA-F]{1,2}|.)", decode, value)


def metadata_rows(lines):
    """Read only routing metadata from pg_restore COPY records; discard content."""
    table, columns = None, []
    for line in lines:
        if line.startswith("COPY "):
            match = re.fullmatch(r"COPY public\.(documents|memory_units) \((.*?)\) FROM stdin;\n?", line)
            table, columns = (match[1], match[2].split(", ")) if match else (None, [])
        elif line.rstrip("\n") == r"\.":
            table = None
        elif table:
            values = line.rstrip("\n").split("\t")
            require(len(values) == len(columns), "invalid_copy_column_count")
            row = dict(zip(columns, values))
            for field in ("metadata", "retain_params"):
                if field not in row:
                    continue
                value = json.loads(copy_value(row[field]) or "{}")
                if field == "retain_params":
                    value = value.get("metadata", {}) if isinstance(value, dict) else {}
                if isinstance(value, dict) and value.get("session_id"):
                    yield table, copy_value(row["id"]), field, value


def inspect(state, dump, pg_restore):
    state, dump = Path(state), Path(dump)
    require(not Path(str(state) + "-wal").exists() or Path(str(state) + "-wal").stat().st_size == 0,
            "checkpointed_offline_snapshot_required")
    before = checksum(state)
    dump_hash = checksum(dump)
    with closing(sqlite3.connect(state.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        unknown = {row[0] for row in db.execute("SELECT id FROM sessions WHERE source='whatsapp' AND COALESCE(chat_id,'')='' AND COALESCE(session_key,'')=''")}
    candidates = {}
    # Errors may contain private dump contents; never forward stderr to terminal.
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen([str(pg_restore), "--data-only", "--file=-", "--table=documents", "--table=memory_units", str(dump)], stdout=subprocess.PIPE, stderr=errors, text=True)
        try:
            for table, row_id, field, metadata in metadata_rows(process.stdout):
                session = metadata["session_id"]
                if session not in unknown or not metadata.get("chat_id"):
                    continue
                route = {key: str(metadata.get(key) or "") for key in ("platform", "chat_id", "chat_type", "thread_id")}
                evidence = {"table": table, "row_id": row_id, "field": field, "metadata_sha256": fingerprint(metadata)}
                entry = candidates.setdefault(session, {"routes": [], "evidence": []})
                if route not in entry["routes"]:
                    entry["routes"].append(route)
                entry["evidence"].append(evidence)
            require(process.wait() == 0, "pg_restore_metadata_failed")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
    require(before == checksum(state) and dump_hash == checksum(dump), "source_changed_during_inspection")
    for entry in candidates.values():
        entry["routes"].sort(key=fingerprint)
        entry["evidence"].sort(key=fingerprint)
    return {"format": "squire-session-provenance-v1", "state_sha256": before, "dump_sha256": dump_hash,
            "untyped_session_count": len(unknown), "candidates": candidates}


def hydrate(state, dump, pg_restore, review, output):
    """Validate review against re-extracted evidence, then create a new offline DB."""
    state, output = Path(state), Path(output)
    require(not output.exists() and not output.is_symlink(), "new_output_database_required")
    observed = inspect(state, dump, pg_restore)
    require(review.get("format") == "squire-session-provenance-review-v1", "review_required")
    require(review.get("state_sha256") == observed["state_sha256"] and review.get("dump_sha256") == observed["dump_sha256"]
            and review.get("candidate_manifest_sha256") == fingerprint(observed), "review_provenance_mismatch")
    selected = review.get("session_ids", [])
    owner_chat = review.get("approved_dm_chat_id")
    require(selected and len(selected) == len(set(selected)) and owner_chat, "explicit_session_and_dm_review_required")
    with closing(sqlite3.connect(state.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        require(db.execute("SELECT 1 FROM sessions WHERE source='whatsapp' AND chat_type='dm' AND chat_id=? LIMIT 1", (owner_chat,)).fetchone(), "approved_chat_not_in_known_dm_history")
        for session in selected:
            entry = observed["candidates"].get(session, {})
            require(entry.get("routes") == [{"platform": "whatsapp", "chat_id": owner_chat, "chat_type": "dm", "thread_id": ""}], "missing_conflicting_or_non_dm_evidence")
            row = db.execute("SELECT source,chat_id,chat_type,session_key,origin_json,thread_id FROM sessions WHERE id=?", (session,)).fetchone()
            require(row and row[0] == "whatsapp" and not any(row[1:]), "existing_routing_identity_conflict")
    output.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=".provenance-", dir=output.parent)
    os.close(handle)
    temporary = Path(name)
    try:
        shutil.copyfile(state, temporary)
        require(checksum(temporary) == observed["state_sha256"], "source_changed_during_copy")
        with closing(sqlite3.connect(temporary)) as db:
            for session in selected:
                db.execute("UPDATE sessions SET chat_id=?,chat_type='dm' WHERE id=?", (owner_chat, session))
            db.commit()
            require(db.execute("PRAGMA quick_check").fetchall() == [("ok",)], "hydrated_database_invalid")
            require(db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0,
                    "hydrated_database_checkpoint_failed")
        require(checksum(state) == observed["state_sha256"], "source_changed_during_hydration")
        # Hard-link publication fails if an output appeared during verification.
        os.link(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return {"format": "squire-session-provenance-result-v1", "hydrated_sessions": len(selected),
            "source_state_sha256": observed["state_sha256"], "source_dump_sha256": observed["dump_sha256"],
            "review_sha256": fingerprint(review), "output_state_sha256": checksum(output),
            "source_unchanged": True, "runtime_start_authorized": False, "requires_private_history_remap": True}


def private_json(path, value):
    path = Path(path)
    with open(path, "x", opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["inspect", "hydrate"])
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--dump", type=Path, required=True)
    parser.add_argument("--pg-restore", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--review", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "inspect":
            result = inspect(args.state, args.dump, args.pg_restore)
            private_json(args.output, result)
            print(json.dumps({"candidate_sessions": len(result["candidates"]), "untyped_sessions": result["untyped_session_count"]}))
        else:
            require(args.review and args.report, "review_and_report_required")
            require(not args.report.exists(), "new_report_required")
            result = hydrate(args.state, args.dump, args.pg_restore, json.loads(args.review.read_text()), args.output)
            private_json(args.report, result)
            print(json.dumps(result))
    except (MigrationError, OSError, ValueError, sqlite3.Error):
        parser.exit(1, "Offline provenance operation failed; no source database was changed.\n")


if __name__ == "__main__":
    main()
