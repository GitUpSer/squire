#!/usr/bin/env python3
"""Read-only integrity diagnostics for explicit database paths; never print rows."""
import argparse
import json
import re
import sqlite3
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--plan", required=True, type=Path)
parser.add_argument("--home", required=True, type=Path)
args = parser.parse_args()
plan = json.loads(args.plan.read_text())
for name in plan["sqlite"]:
    try:
        with sqlite3.connect(f"file:{args.home / name}?mode=ro", uri=True, timeout=5) as db:
            rows = [row[0] for row in db.execute("PRAGMA integrity_check(20)")]
        safe = [value if value == "ok" or re.fullmatch(r"(?:row [0-9]+ missing from index [\w]+|wrong # of entries in index [\w]+|NULL value in [\w.]+|non-unique entry in index [\w]+)", value)
                else "integrity_failure_redacted" for value in rows]
        classes = sorted({word for value in rows for word in ("malformed", "page", "freelist", "index", "rowid", "missing", "NULL", "unique", "CHECK", "unused", "fragmentation", "btree", "datatype") if word.lower() in value.lower()})
        print(json.dumps({"database": name, "integrity": safe, "error_classes": classes, "sqlite_version": sqlite3.sqlite_version}), flush=True)
    except sqlite3.Error:
        print(json.dumps({"database": name, "error": "sqlite_check_failed"}), flush=True)
