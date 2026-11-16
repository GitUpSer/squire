#!/usr/bin/env python3
"""Restore only into the dedicated disposable Railway PG18 rehearsal service."""
import argparse
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

RESTORE_SERVICE_ID = "05391445-8c9a-4cda-a823-73f1468891e6"
DATABASES = {"control": "squire_drill_control", "messaging": "squire_drill_messaging",
             "litellm": "squire_drill_litellm", "hindsight": "squire_drill_hindsight",
             "hindsight-source": "squire_drill_hindsight_source"}


class DrillError(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise DrillError(code)


def run(argv, env, *, timeout=1200):
    result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=timeout)
    # pg_restore/SQL errors can contain data. Keep full diagnostics in private
    # local files for the operator, and emit only an error code to public logs.
    if result.returncode:
        destination = Path("/tmp/squire-restore-drill-error.log")
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(result.stderr)
        raise DrillError("restore_command_failed_private_diagnostic_available")
    return result.stdout.strip()


def connection(database="postgres"):
    require(os.environ.get("RAILWAY_SERVICE_ID") == RESTORE_SERVICE_ID, "not_the_isolated_restore_service")
    port = os.environ.get("SQUIRE_DRILL_PG_PORT", "5432")
    require(port in {"5432", "55432"}, "invalid_isolated_drill_port")
    env = dict(os.environ)
    for name in list(env):
        if name.startswith("PG"):
            del env[name]
    env.update(PGHOST="127.0.0.1", PGPORT=port, PGUSER=os.environ.get("POSTGRES_USER", "postgres"),
               PGPASSWORD=os.environ.get("POSTGRES_PASSWORD", ""), PGDATABASE=database, PGCONNECT_TIMEOUT="10",
               PGOPTIONS="-c max_parallel_maintenance_workers=0 -c max_parallel_workers_per_gather=0 -c maintenance_work_mem=64MB")
    return env


def sql(statement, env):
    return run(["psql", "--no-psqlrc", "--no-password", "--set=ON_ERROR_STOP=1", "-Atc", statement], env)


def identifier(value):
    return '"' + value.replace('"', '""') + '"'


def inventory(database):
    require(database in DATABASES.values(), "invalid_drill_database")
    env = connection(database)
    tables = json.loads(sql("SELECT COALESCE(json_agg(json_build_array(schemaname,tablename) ORDER BY schemaname,tablename),'[]') FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema')", env))
    counts = {}
    for schema, table in tables:
        counts[schema + "." + table] = int(sql("SELECT count(*) FROM " + identifier(schema) + "." + identifier(table), env))
    columns = sql("SELECT COALESCE(json_agg(json_build_array(table_schema,table_name,column_name,data_type,is_nullable) ORDER BY table_schema,table_name,ordinal_position),'[]') FROM information_schema.columns WHERE table_schema NOT IN ('pg_catalog','information_schema')", env)
    extensions = json.loads(sql("SELECT COALESCE(json_object_agg(extname,extversion),'{}') FROM pg_extension", env))
    revision = sql("SELECT version_num FROM public.alembic_version", env) if "public.alembic_version" in counts else None
    return {"database": database, "tables": len(counts), "row_counts": counts, "column_schema_sha256": hashlib.sha256(columns.encode()).hexdigest(),
            "extensions": extensions, "alembic_revision": revision}


def restore(kind, archive, expected_sha256):
    require(kind in DATABASES, "invalid_database_kind")
    require(archive.is_file() and not archive.is_symlink(), "regular_dump_required")
    digest = hashlib.file_digest(archive.open("rb"), "sha256").hexdigest()
    require(digest == expected_sha256, "dump_checksum_mismatch")
    env = connection()
    version = int(sql("SHOW server_version_num", env))
    require(180000 <= version < 190000, "postgres_18_required")
    database = DATABASES[kind]
    exists = sql("SELECT 1 FROM pg_database WHERE datname='" + database + "'", env)
    require(not exists, "drill_database_already_exists_no_overwrite")
    if kind.startswith("hindsight"):
        available = json.loads(sql("SELECT COALESCE(json_object_agg(name,default_version),'{}') FROM pg_available_extensions WHERE name IN ('vector','pg_trgm')", env))
        require(set(available) == {"vector", "pg_trgm"}, "hindsight_extensions_missing")
    sql("CREATE DATABASE " + identifier(database) + " ENCODING 'UTF8' TEMPLATE template0", env)
    run(["pg_restore", "--no-password", "--exit-on-error", "--no-owner", "--no-acl", "--dbname", database, str(archive)], connection(database))
    result = inventory(database)
    result.update(kind=kind, dump_sha256=digest, restored=True, at=time.time(), isolated_service=RESTORE_SERVICE_ID)
    return result


def archive_failed(kind):
    """Keep an unsuccessful disposable restore for inspection; never DROP it."""
    require(kind in DATABASES, "invalid_database_kind")
    env = connection()
    database = DATABASES[kind]
    archived = database + "_failed_" + str(int(time.time()))
    sql("ALTER DATABASE " + identifier(database) + " RENAME TO " + identifier(archived), env)
    return {"preserved_failed_database": archived, "ready_for_fresh_restore": database}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["restore", "inventory", "archive-failed"])
    parser.add_argument("--kind", choices=list(DATABASES), required=True)
    parser.add_argument("--dump", type=Path)
    parser.add_argument("--sha256")
    args = parser.parse_args()
    try:
        if args.command == "restore":
            require(args.dump and args.sha256, "dump_and_checksum_required")
            result = restore(args.kind, args.dump, args.sha256)
        elif args.command == "archive-failed":
            result = archive_failed(args.kind)
        else:
            result = inventory(DATABASES[args.kind])
        print(json.dumps(result, sort_keys=True))
    except DrillError as exc:
        raise SystemExit(str(exc)) from None
    except Exception:
        raise SystemExit("isolated_restore_drill_failed") from None
