#!/usr/bin/env python3
"""Preserve pristine old memory tables beside the disposable upgraded database."""
import argparse
import collections
import json
import os

import psycopg2
from psycopg2 import sql

from pilot_restore_drill import DATABASES, connection, require
from pilot_hindsight_upgrade import private_json


def connect(kind):
    env = connection(DATABASES[kind])
    require(env["PGPORT"] == "55432", "archive_requires_matched_disposable_cluster")
    return psycopg2.connect(host=env["PGHOST"], port=env["PGPORT"], user=env["PGUSER"],
        password=env["PGPASSWORD"], dbname=env["PGDATABASE"], options=env["PGOPTIONS"])


def fingerprints(cursor, schema):
    cursor.execute(sql.SQL("SELECT id::text,fact_type,md5(row_to_json(record)::text) FROM (SELECT id,bank_id,text,context,fact_type FROM {}.memory_units) record").format(sql.Identifier(schema)))
    return {row[0]: (row[1], row[2]) for row in cursor.fetchall()}


def archive(report):
    with connect("hindsight-source") as source, connect("hindsight") as target:
        with source.cursor() as src, target.cursor() as dst:
            src.execute("SELECT version_num FROM public.alembic_version")
            require(src.fetchone()[0] == "h3i4j5k6l7m8", "source_baseline_revision_changed")
            dst.execute("SELECT version_num FROM public.alembic_version")
            require(dst.fetchone()[0] == "b3e8d1c6f4a9", "upgraded_revision_unexpected")
            before, after = fingerprints(src, "public"), fingerprints(dst, "public")
            lost = collections.Counter(before[key][0] for key in before.keys() - after.keys())
            changed = sum(before[key] != after[key] for key in before.keys() & after.keys())
            src.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
            names = [row[0] for row in src.fetchall()]
            dst.execute("SELECT 1 FROM pg_namespace WHERE nspname='legacy_v052'")
            require(dst.fetchone() is None, "legacy_archive_already_exists_no_overwrite")
            dst.execute("CREATE EXTENSION IF NOT EXISTS postgres_fdw")
            dst.execute("CREATE SCHEMA legacy_source_fdw")
            dst.execute("CREATE SCHEMA legacy_v052")
            dst.execute("REVOKE ALL ON SCHEMA legacy_v052 FROM PUBLIC")
            dst.execute("CREATE SERVER squire_legacy_source FOREIGN DATA WRAPPER postgres_fdw OPTIONS(host '127.0.0.1',port '55432',dbname 'squire_drill_hindsight_source')")
            dst.execute("CREATE USER MAPPING FOR CURRENT_USER SERVER squire_legacy_source OPTIONS(user 'postgres')")
            dst.execute("IMPORT FOREIGN SCHEMA public FROM SERVER squire_legacy_source INTO legacy_source_fdw")
            rows = {}
            for name in names:
                dst.execute(sql.SQL("CREATE TABLE legacy_v052.{} AS SELECT * FROM legacy_source_fdw.{}").format(sql.Identifier(name), sql.Identifier(name)))
                dst.execute(sql.SQL("SELECT count(*) FROM legacy_v052.{}").format(sql.Identifier(name)))
                copied = dst.fetchone()[0]
                src.execute(sql.SQL("SELECT count(*) FROM public.{}").format(sql.Identifier(name)))
                require(copied == src.fetchone()[0], "legacy_archive_count_mismatch")
                rows[name] = copied
            require(fingerprints(dst, "legacy_v052") == before, "legacy_memory_hash_mismatch")
            dst.execute("DROP SERVER squire_legacy_source CASCADE")
            dst.execute("DROP SCHEMA legacy_source_fdw")
            target.commit()
    result = {"archived_schema": "legacy_v052", "archived_tables": len(rows), "row_counts": rows,
              "deleted_memory_types": dict(lost), "surviving_memory_content_changes": changed,
              "new_memory_rows": len(after.keys() - before.keys()), "original_memory_hashes_preserved": True,
              "archive_is_not_active_recall_parity": True, "source_vps_changed": False}
    private_json(report, result)
    return result


if __name__ == "__main__":
    from pathlib import Path
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(archive(args.report), sort_keys=True))
    except Exception:
        raise SystemExit("isolated_legacy_archive_failed") from None
