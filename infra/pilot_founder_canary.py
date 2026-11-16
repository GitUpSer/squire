#!/usr/bin/env python3
"""Restore the founder dump only in the named, credential-free canary.

Runs as hermes under the image's Hindsight interpreter. Never drops a database,
never uses --clean, and never starts a gateway, memory worker, or model provider.
Full diagnostics and PostgreSQL connection material stay in owner-only files.
"""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import traceback
from urllib.parse import quote

SERVICE = "59d36ae4-d735-4eca-ae2b-629d48860e57"
RUNTIME = "pilot-canary-founder-b14d9d81d98d"
INSTANCE = "squire-founder-canary"
DATABASE = "squire_founder_canary"
PORT = 55431
USERNAME = "squire_canary"
PROFILE = "reference"
HOME = Path("/opt/data")
DATA = HOME / ".pg0/instances" / INSTANCE / "data"
PRIVATE = HOME / ".squire/founder-canary"
DUMP_SHA = "a7d9f9dd4e5fa28200245e28cdc6b49ddbc58eae3e122b919910cb7504945b58"


class CanaryError(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise CanaryError(code)


def activate_profile(profile):
    """Only these two isolated profiles exist; arbitrary cluster/DB overrides do not."""
    global INSTANCE, DATABASE, PORT, DATA, PRIVATE, USERNAME, PROFILE
    require(profile in {"reference", "embedded-managed"}, "unknown_isolated_profile")
    PROFILE = profile
    if profile == "embedded-managed":
        INSTANCE = "squire-founder-managed"
        DATABASE = "hindsight"
        PORT = 55432
        USERNAME = "hindsight"
        PRIVATE = HOME / ".squire/founder-canary/managed"
    else:
        INSTANCE = "squire-founder-canary"
        DATABASE = "squire_founder_canary"
        PORT = 55431
        USERNAME = "squire_canary"
        PRIVATE = HOME / ".squire/founder-canary"
    DATA = HOME / ".pg0/instances" / INSTANCE / "data"


def gateway_argv(argv):
    return any(b"gateway.run" in x or x.rsplit(b"/", 1)[-1] == b"squire-gateway-run.sh" for x in argv) or any(a == b"gateway" and b == b"run" for a, b in zip(argv, argv[1:]))


def guard(image, *, allow_managed_read=False):
    require(os.environ.get("RAILWAY_SERVICE_ID") == SERVICE, "wrong_canary_service")
    require(os.environ.get("SQUIRE_RUNTIME_ID") == RUNTIME, "wrong_canary_runtime")
    require(os.environ.get("SQUIRE_RUNTIME_KIND") == "dm", "private_canary_only")
    require(os.environ.get("SQUIRE_HINDSIGHT_VARIANT") == "founder", "founder_image_required")
    require(re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", image or "") is not None and os.environ.get("SQUIRE_IMAGE_REF") == image, "wrong_immutable_image")
    require(os.environ.get("HINDSIGHT_API_WORKER_ENABLED") == "false", "memory_worker_must_be_disabled")
    require(os.environ.get("HINDSIGHT_API_LLM_PROVIDER") == "none", "memory_model_must_be_disabled")
    require(os.environ.get("HOME") == str(HOME) and os.environ.get("HERMES_HOME") == str(HOME), "wrong_canary_home")
    require(os.geteuid() != 0, "run_as_hermes_not_root")
    require(importlib.metadata.version("hindsight-api-slim") == "0.5.2", "wrong_hindsight_version")
    require(importlib.metadata.version("pg0-embedded") == "0.12.3", "wrong_pg0_version")
    adopted_read = allow_managed_read and PROFILE == "embedded-managed" and (HOME / ".squire/initialized").is_file()
    for filename in ("auth.json", ".env"):
        p = HOME / filename
        content = p.read_text().strip() if p.exists() else ""
        # Native entrypoint generates this local webhook verification token even
        # for Squire-only homes. It is not a source/provider credential.
        generated_only = filename == ".env" and adopted_read and re.fullmatch(r"TELEGRAM_WEBHOOK_SECRET=[0-9a-f]{64}", content)
        require(not content or generated_only, "owner_credentials_forbidden")
    forbidden = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_OAUTH_TOKEN", "GOOGLE_API_KEY", "GEMINI_API_KEY", "COHERE_API_KEY", "HINDSIGHT_API_LLM_API_KEY", "HINDSIGHT_API_RETAIN_LLM_API_KEY", "HINDSIGHT_API_REFLECT_LLM_API_KEY", "HINDSIGHT_API_CONSOLIDATION_LLM_API_KEY", "RAILWAY_API_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
    require(not any(os.environ.get(k) for k in forbidden), "external_credentials_forbidden")
    # Inspect argv internally; never print any process arguments.
    for proc in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            argv = proc.read_bytes().split(b"\0")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        require(not gateway_argv(argv) or adopted_read, "gateway_must_remain_stopped")
    PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    PRIVATE.chmod(0o700)


def private_json(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)


def run(argv, env):
    result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=1800)
    if result.returncode:
        fd = os.open(PRIVATE / "command-error.log", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(result.stderr)
        raise CanaryError("command_failed_private_diagnostic_available")
    return result.stdout.strip()


def connection(database=None):
    database = DATABASE if database is None else database
    record = json.loads((PRIVATE / "connection.json").read_text())
    require(record["instance"] == INSTANCE and record["port"] == PORT and record["database"] == DATABASE, "wrong_connection_record")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    env.update(PGHOST="127.0.0.1", PGPORT=str(PORT), PGUSER=record["username"], PGPASSWORD=record["password"], PGDATABASE=database, PGCONNECT_TIMEOUT="10", PGOPTIONS="-c max_parallel_maintenance_workers=0 -c max_parallel_workers_per_gather=0 -c maintenance_work_mem=64MB")
    return env


def binaries():
    pid = int((DATA / "postmaster.pid").read_text().splitlines()[0])
    binary = Path(f"/proc/{pid}/exe").resolve()
    require(binary.name == "postgres", "expected_postgres_process")
    return binary.parent


def sql(statement, database=None):
    database = DATABASE if database is None else database
    return run([str(binaries() / "psql"), "--no-psqlrc", "--no-password", "--set=ON_ERROR_STOP=1", "-Atc", statement], connection(database))


def identifier(value):
    return '"' + value.replace('"', '""') + '"'


def verify_server():
    require(sql("SHOW data_directory", "postgres") == str(DATA), "wrong_embedded_cluster")
    version = int(sql("SHOW server_version_num", "postgres"))
    require(180000 <= version < 190000, "postgres18_required")


def bootstrap():
    require(not (PRIVATE / "connection.json").exists() and not DATA.exists(), "canary_cluster_already_exists_no_overwrite")
    from pg0 import Pg0
    password = "hindsight" if PROFILE == "embedded-managed" else secrets.token_urlsafe(32)
    private_json(PRIVATE / "connection.json", {"instance": INSTANCE, "port": PORT, "database": DATABASE, "username": USERNAME, "password": password})
    pg = Pg0(name=INSTANCE, port=PORT, username=USERNAME, password=password, database="postgres", data_dir=str(DATA))
    info = pg.start()
    require(info.running and info.port == PORT and Path(info.data_dir) == DATA, "unexpected_pg0_instance")
    verify_server()
    return {"embedded_cluster_started": True, "postgres_major": 18, "port": PORT}


def inventory():
    verify_server()
    tables = json.loads(sql("SELECT coalesce(json_agg(json_build_array(schemaname,tablename) ORDER BY schemaname,tablename),'[]') FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema')"))
    result = {}
    for schema, table in tables:
        name = identifier(schema) + "." + identifier(table)
        value = json.loads(sql("SELECT json_build_object('rows',count(*),'full_rows_md5',md5(coalesce(string_agg(digest,'' ORDER BY digest),''))) FROM (SELECT md5(row_to_json(r)::text) digest FROM " + name + " r) hashes"))
        result[schema + "." + table] = value
    overrides = json.loads(sql("SELECT coalesce(json_agg(config),'[]') FROM public.banks"))
    require(len(overrides) == 1 and all(not x for x in overrides), "bank_overrides_require_separate_review")
    queue = json.loads(sql("SELECT coalesce(json_object_agg(status,n),'{}') FROM (SELECT status,count(*) n FROM public.async_operations GROUP BY status) q"))
    require(not queue.get("pending") and not queue.get("processing"), "source_queue_not_quiescent")
    extensions = json.loads(sql("SELECT json_object_agg(extname,extversion) FROM pg_extension"))
    columns = sql("SELECT coalesce(json_agg(json_build_array(table_schema,table_name,column_name,data_type,is_nullable) ORDER BY table_schema,table_name,ordinal_position),'[]') FROM information_schema.columns WHERE table_schema NOT IN ('pg_catalog','information_schema')")
    return {"database": DATABASE, "tables": result, "column_schema_sha256": hashlib.sha256(columns.encode()).hexdigest(), "bank_override_keys": [], "bank_identity_sha256": hashlib.sha256(sql("SELECT bank_id FROM public.banks").encode()).hexdigest(), "queue_counts": queue, "extensions": extensions, "revision": sql("SELECT version_num FROM public.alembic_version")}


def restore(archive):
    verify_server()
    require(archive.is_file() and not archive.is_symlink(), "regular_dump_required")
    with archive.open("rb") as stream:
        require(hashlib.file_digest(stream, "sha256").hexdigest() == DUMP_SHA, "dump_checksum_mismatch")
    require(not sql("SELECT 1 FROM pg_database WHERE datname='" + DATABASE + "'", "postgres"), "restore_database_exists_no_overwrite")
    available = json.loads(sql("SELECT json_agg(name) FROM pg_available_extensions WHERE name IN ('vector','pg_trgm')", "postgres"))
    require(set(available) == {"vector", "pg_trgm"}, "required_extensions_missing")
    sql("CREATE DATABASE " + identifier(DATABASE) + " ENCODING 'UTF8' TEMPLATE template0", "postgres")
    run([str(binaries() / "pg_restore"), "--no-password", "--exit-on-error", "--no-owner", "--no-acl", "--dbname", DATABASE, str(archive)], connection())
    report = inventory()
    require(len(report["tables"]) == 15 and report["tables"]["public.memory_units"]["rows"] == 14324 and report["tables"]["public.documents"]["rows"] == 1132 and report["tables"]["public.entities"]["rows"] == 5228, "source_inventory_mismatch")
    require(report["revision"] == "h3i4j5k6l7m8", "source_revision_mismatch")
    private_json(PRIVATE / "restored-baseline.json", report)
    return {"restored": True, "dump_sha256": DUMP_SHA, "inventory": report}


def stop_managed():
    """Hand a fully verified, stopped cluster to upstream's own lifecycle."""
    require(PROFILE == "embedded-managed", "only_managed_cluster_may_be_stopped")
    review()
    from pg0 import Pg0
    pg = Pg0(name=INSTANCE)
    require(pg.info().running, "managed_cluster_not_running")
    pg.stop()
    return finalize_managed_stop()


def finalize_managed_stop():
    """Remove only stale process metadata after a proven clean managed shutdown.

    The sleep-only staging PID1 does not reap orphaned PostgreSQL children;
    pg0 0.12.3's kill-0 probe incorrectly treats those zombies as running.
    A real process, nonempty postmaster.pid, or unclean control state fails shut.
    """
    require(PROFILE == "embedded-managed", "only_managed_cluster_may_be_stopped")
    metadata = DATA.parent / "instance.json"
    require(metadata.is_file() and not metadata.is_symlink(), "managed_instance_metadata_required")
    info = json.loads(metadata.read_text())
    require(info.get("data_dir") == str(DATA) and info.get("username") == USERNAME and info.get("database") in {"postgres", DATABASE}, "managed_instance_metadata_mismatch")
    require(not (DATA / "postmaster.pid").exists(), "managed_postmaster_still_present")
    proc = Path("/proc") / str(int(info["pid"])) / "stat"
    require(not proc.exists() or proc.read_text().rsplit(")", 1)[1].split()[0] == "Z", "managed_pid_is_not_stopped")
    installation = Path(info["installation_dir"]).resolve()
    require(installation == (HOME / ".pg0/installation").resolve(), "unexpected_pg0_installation")
    controls = list(installation.rglob("pg_controldata"))
    require(len(controls) == 1, "unambiguous_pg_control_binary_required")
    control = run([str(controls[0]), str(DATA)], dict(os.environ, LC_ALL="C"))
    state = next((line.split(":", 1)[1].strip() for line in control.splitlines() if line.startswith("Database cluster state:")), None)
    require(state == "shut down", "managed_cluster_not_cleanly_shutdown")
    require((DATA / "PG_VERSION").read_text().strip() == "18", "managed_data_version_missing")
    baseline = json.loads((PRIVATE / "restored-baseline.json").read_text())
    require(baseline["tables"]["public.memory_units"]["rows"] == 14324, "managed_restore_baseline_missing")
    files = {}
    for path in sorted(DATA.rglob("*")):
        require(not path.is_symlink(), "managed_data_symlink_forbidden")
        if path.is_file():
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            files[str(path.relative_to(DATA))] = {"bytes": path.stat().st_size, "sha256": digest}
    total_bytes = sum(item["bytes"] for item in files.values())
    require(total_bytes > 100_000_000, "managed_data_unexpectedly_small")
    data_hash = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    private_json(PRIVATE / "stopped-data-manifest.json", {"files": files, "total_bytes": total_bytes, "sha256": data_hash})
    archived = PRIVATE / "stopped-instance.json"
    require(not archived.exists(), "managed_metadata_archive_exists")
    os.replace(metadata, archived)
    archived.chmod(0o600)
    return {"managed_cluster_stopped": True, "native_database_url": "pg0://" + INSTANCE + ":" + str(PORT), "data_directory": str(DATA), "database": DATABASE, "reference_cluster_untouched": True, "managed_data_bytes": total_bytes, "managed_data_files": len(files), "managed_data_manifest_sha256": data_hash, "clean_shutdown_verified": True, "stale_process_metadata_archived": True}


def serve():
    require(PROFILE == "reference", "managed_profile_must_use_normal_hindsight_binary")
    before = inventory()
    require(before == json.loads((PRIVATE / "restored-baseline.json").read_text()), "restored_baseline_changed_before_start")
    env = connection()
    url = "postgresql://" + quote(env["PGUSER"], safe="") + ":" + quote(env["PGPASSWORD"], safe="") + "@127.0.0.1:" + str(PORT) + "/" + DATABASE
    os.environ.update(HINDSIGHT_API_DATABASE_URL=url, HINDSIGHT_API_AUDIT_LOG_ENABLED="false", HINDSIGHT_API_WORKER_ENABLED="false", HINDSIGHT_API_LLM_PROVIDER="none", HINDSIGHT_API_RERANKER_PROVIDER="rrf", HINDSIGHT_API_SKIP_LLM_VERIFICATION="true", HINDSIGHT_API_EMBEDDINGS_PROVIDER="local", HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL="BAAI/bge-small-en-v1.5")
    import logging
    logging.disable(logging.CRITICAL)
    import uvicorn
    from hindsight_api import MemoryEngine
    from hindsight_api.api import create_app
    from hindsight_api.engine.embeddings import LocalSTEmbeddings
    from hindsight_api.engine.cross_encoder import RRFPassthroughCrossEncoder
    memory = MemoryEngine(db_url=url, memory_llm_provider="none", embeddings=LocalSTEmbeddings(model_name="BAAI/bge-small-en-v1.5", force_cpu=True), cross_encoder=RRFPassthroughCrossEncoder(), skip_llm_verification=True, pool_min_size=1, pool_max_size=5)
    uvicorn.run(create_app(memory), host="127.0.0.1", port=9177, access_log=False, log_level="critical")


def recall():
    # Query text stays in the process pipe; it never appears in argv or reports.
    bank = sql("SELECT bank_id FROM public.banks")
    sample = json.loads(sql("SELECT row_to_json(r) FROM (SELECT id::text, text, fact_type FROM public.memory_units WHERE fact_type IN ('world','experience') AND length(text)>40 ORDER BY id LIMIT 1) r"))
    request = json.dumps({"bank_id": bank, "query": sample["text"][:1000], "fact_type": sample["fact_type"], "expected_id": sample["id"]})
    client_code = r"""
import hashlib,json,sys
from importlib.metadata import version
from hindsight_client import Hindsight
assert version('hindsight-client') == '0.10.3'
v=json.load(sys.stdin)
with Hindsight(base_url='http://127.0.0.1:9177',timeout=120) as client:
    recalled=client.recall(bank_id=v['bank_id'],query=v['query'],types=[v['fact_type']],budget='mid',max_tokens=4096)
    rows=[item.model_dump(mode='json') for item in recalled.results]
    found=any(str(item.get('id')) == v['expected_id'] for item in rows)
    assert rows and found, 'known_source_fact_not_recalled'
    print(json.dumps({'client_version':'0.10.3','results':len(rows),'known_source_fact_recalled':found,'results_sha256':hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest()}))
"""
    result = subprocess.run(["/opt/hermes/.venv/bin/python", "-c", client_code], input=request, capture_output=True, text=True, timeout=180)
    if result.returncode:
        fd = os.open(PRIVATE / "recall-error.log", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(result.stderr)
        raise CanaryError("recall_failed_private_diagnostic_available")
    report = json.loads(result.stdout)
    report.update(review())
    report.update(embeddings="BAAI/bge-small-en-v1.5", embedding_dimensions=384, reranker="rrf", model_calls=False)
    private_json(PRIVATE / "recall-report.json", report)
    return report


def review():
    after = inventory()
    baseline = json.loads((PRIVATE / "restored-baseline.json").read_text())
    require(after == baseline, "database_content_or_schema_changed_after_recall")
    return {"all_table_counts_and_full_row_hashes_unchanged": True, "tables": len(after["tables"]), "bank_identity_sha256": after["bank_identity_sha256"], "worker_enabled": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["bootstrap", "restore", "inventory", "serve", "recall", "review", "stop-managed", "finalize-managed-stop"])
    parser.add_argument("--profile", choices=["reference", "embedded-managed"], default="reference")
    parser.add_argument("--image-ref", required=True)
    parser.add_argument("--dump", type=Path)
    args = parser.parse_args()
    activate_profile(args.profile)
    guard(args.image_ref, allow_managed_read=args.command in {"inventory", "recall", "review"})
    if args.command == "serve":
        serve()
        return
    if args.command == "restore":
        require(args.dump is not None, "dump_required")
        result = restore(args.dump)
    else:
        result = {"bootstrap": bootstrap, "inventory": inventory, "recall": recall, "review": review, "stop-managed": stop_managed, "finalize-managed-stop": finalize_managed_stop}[args.command]()
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        if PRIVATE.exists():
            fd = os.open(PRIVATE / "helper-error.log", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as stream:
                traceback.print_exc(file=stream)
        raise SystemExit(str(exc) if isinstance(exc, CanaryError) else "founder_canary_failed_private_diagnostic_available") from None
