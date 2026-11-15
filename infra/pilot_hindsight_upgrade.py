#!/usr/bin/env python3
"""Run pinned Hindsight DDL only in the disposable restore database."""
import argparse
import hashlib
import importlib.machinery
import json
import logging
import os
import sys
import tempfile
import traceback
import types
import zipfile
from pathlib import Path
from urllib.parse import quote

try:
    from pilot_restore_drill import DATABASES, DrillError, connection, identifier, inventory, require, sql
except ModuleNotFoundError:
    from drill import DATABASES, DrillError, connection, identifier, inventory, require, sql

WHEEL_SHA256 = "d3ed928e281aca808dfebe304b75d62f333c10d9700fb09b69c7fb35f004854b"
WHEEL_HASHES = {"0.9.0": WHEEL_SHA256, "0.5.2": "794a4387c3bef263642ae710c4d36cba52c2cdd2c237f5c61633921d303e1b3c"}


def private_json(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)


def content_signatures(env):
    # Hash stable identity/content columns inside PostgreSQL. No memory text or
    # bank identifiers leave the database in these verification reports.
    columns = {"banks": ["bank_id"], "documents": ["bank_id", "id", "original_text"],
               "memory_units": ["bank_id", "id", "text", "context"],
               "entities": ["bank_id", "id", "canonical_name"]}
    result = {}
    for table, desired in columns.items():
        available = json.loads(sql("SELECT COALESCE(json_agg(column_name),'[]') FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='" + table + "'", env))
        selected = [name for name in desired if name in available]
        require(selected and len(selected) >= min(2, len(desired)), "core_memory_columns_missing")
        projection = ",".join(identifier(name) for name in selected)
        query = "SELECT md5(COALESCE(string_agg(digest, '' ORDER BY digest),'')) FROM (SELECT md5(row_to_json(record)::text) digest FROM (SELECT " + projection + " FROM public." + identifier(table) + ") record) hashes"
        result[table] = {"columns": selected, "content_digest": sql(query, env)}
    return result


def namespace(name, path):
    # The upstream __init__ files eagerly import the full model/provider runtime.
    # Expose only their package paths, leaving all migration/config/model module
    # code unmodified. This cannot start an API, worker, recall or model request.
    module = types.ModuleType(name)
    module.__path__ = [str(path)]
    module.__package__ = name
    module.__spec__ = importlib.machinery.ModuleSpec(name, loader=None, is_package=True)
    sys.modules[name] = module


def upgrade(wheel, output, version="0.9.0", kind="hindsight"):
    require(kind == "hindsight" or version == "0.5.2", "source_baseline_may_only_run_matching_version")
    database = DATABASES[kind]
    env = connection(database)
    require(not output.exists(), "upgrade_report_already_exists")
    with wheel.open("rb") as stream:
        require(hashlib.file_digest(stream, "sha256").hexdigest() == WHEEL_HASHES[version], "unverified_hindsight_wheel")
    before = inventory(database)
    require(before["alembic_revision"] == "h3i4j5k6l7m8", "unexpected_source_revision")
    signatures = content_signatures(env)
    private_json(output.with_suffix(".before.json"), {"inventory": before, "content_signatures": signatures})
    for key in list(os.environ):
        if key.startswith("HINDSIGHT_"):
            del os.environ[key]
    url = "postgresql://" + quote(env["PGUSER"], safe="") + ":" + quote(env["PGPASSWORD"], safe="") + "@127.0.0.1:" + env["PGPORT"] + "/" + database
    os.environ["HINDSIGHT_API_DATABASE_URL"] = url
    os.environ["HINDSIGHT_API_VECTOR_EXTENSION"] = "pgvector"
    os.environ["PGOPTIONS"] = env["PGOPTIONS"]
    logging.disable(logging.CRITICAL)
    with tempfile.TemporaryDirectory(prefix="squire-hindsight-code-") as directory:
        root = Path(directory)
        with zipfile.ZipFile(wheel) as archive:
            require(all(not Path(name).is_absolute() and ".." not in Path(name).parts for name in archive.namelist()), "invalid_wheel_path")
            archive.extractall(root)
        package = root / "hindsight_api"
        namespace("hindsight_api", package)
        namespace("hindsight_api.engine", package / "engine")
        namespace("hindsight_api.engine.retain", package / "engine/retain")
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        cfg = Config()
        cfg.set_main_option("script_location", str(package / "alembic"))
        revisions = ScriptDirectory.from_config(cfg)
        require(revisions.get_revision(before["alembic_revision"]) is not None, "source_revision_unknown")
        expected_heads = set(revisions.get_heads())
        from hindsight_api.migrations import run_migrations
        run_migrations(url)
        actual_heads = set(sql("SELECT version_num FROM public.alembic_version ORDER BY version_num", env).splitlines())
        require(actual_heads == expected_heads, "upgrade_did_not_reach_exact_heads")
    after = inventory(database)
    after_signatures = content_signatures(env)
    private_json(output.with_suffix(".after.json"), {"inventory": after, "content_signatures": after_signatures})
    require(signatures == after_signatures, "core_memory_content_changed_requires_review")
    for table in signatures:
        require(after["row_counts"].get("public." + table) == before["row_counts"].get("public." + table),
                "core_memory_row_count_changed")
    result = {"migration_only": True, "model_or_worker_started": False, "hindsight_version": version,
              "wheel_sha256": WHEEL_HASHES[version], "before": before, "after": after,
              "core_memory_content_unchanged": True, "exact_heads_verified": True,
              "full_runtime_recall_parity_still_required": True}
    private_json(output, result)
    return {"upgraded": True, "version": version, "from_revision": before["alembic_revision"],
            "to_revision": after["alembic_revision"], "core_memory_content_unchanged": True,
            "tables_before": before["tables"], "tables_after": after["tables"], "report": str(output)}


def review(output):
    before = json.loads(output.with_suffix(".before.json").read_text())
    after = inventory(DATABASES["hindsight"])
    signatures = content_signatures(connection(DATABASES["hindsight"]))
    result = {"revision": after["alembic_revision"], "tables": after["tables"], "changed_core_tables": [
        name for name, old in before["content_signatures"].items() if old != signatures[name]],
        "core_counts": {name: {"before": before["inventory"]["row_counts"].get("public." + name),
                               "after": after["row_counts"].get("public." + name)} for name in signatures}}
    private_json(output.with_suffix(".review.json"), {"review": result, "after_inventory": after,
                                                     "after_signatures": signatures})
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--review-only", action="store_true")
    parser.add_argument("--version", choices=list(WHEEL_HASHES), default="0.9.0")
    parser.add_argument("--kind", choices=["hindsight", "hindsight-source"], default="hindsight")
    args = parser.parse_args()
    try:
        print(json.dumps(review(args.report) if args.review_only else upgrade(args.wheel, args.report, args.version, args.kind)))
    except BaseException as exc:
        fd = os.open("/tmp/squire-hindsight-upgrade-error.log", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            traceback.print_exc(file=stream)
        raise SystemExit(str(exc) if isinstance(exc, DrillError) else "hindsight_upgrade_failed_private_diagnostic_available") from None
