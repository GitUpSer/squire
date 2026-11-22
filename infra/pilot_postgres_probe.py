#!/usr/bin/env python3
"""Read-only connection discovery. Credentials never appear in stdout or argv."""
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit


def probe(home=Path.home()):
    base = home / ".pg0/instances/hindsight-embed-hermes"
    result = {"instance_files": [], "daemon_database_env_keys": [], "candidate_config_keys": {}}
    candidates = []
    if base.exists():
        for path in sorted(base.iterdir()):
            if path.is_file() and not path.is_symlink() and path.stat().st_size < 1024 * 1024:
                result["instance_files"].append(path.name)
                if path.suffix == ".json":
                    try:
                        value = json.loads(path.read_text())
                        if isinstance(value, dict):
                            result["candidate_config_keys"][path.name] = sorted(value)
                            candidates.append((str(path), value))
                    except (ValueError, OSError):
                        pass
    process = subprocess.run(["systemctl", "--user", "show", "hindsight-daemon.service", "--property=MainPID", "--value"],
                             capture_output=True, text=True, check=False)
    try:
        pid = int(process.stdout.strip())
        environment = dict(item.split(b"=", 1) for item in (Path("/proc") / str(pid) / "environ").read_bytes().split(b"\0") if b"=" in item)
        embedding_keys = ("HINDSIGHT_API_EMBEDDINGS_PROVIDER", "HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL",
            "HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL", "HINDSIGHT_API_EMBEDDINGS_ONNX_MODEL_ID",
            "HINDSIGHT_API_EMBEDDINGS_ONNX_FILE", "HINDSIGHT_API_EMBEDDING_DIMENSION",
            "HINDSIGHT_API_EMBEDDINGS_ONNX_NORMALIZE", "HINDSIGHT_API_RERANKER_PROVIDER")
        result["memory_model_settings"] = {key: environment[key.encode()].decode() for key in embedding_keys
            if key.encode() in environment and re.fullmatch(r"[A-Za-z0-9_./:+-]{1,200}", environment[key.encode()].decode())}
        argv = (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0")
        interpreter = Path(argv[0].decode())
        if interpreter.name.startswith("python") and interpreter.is_file():
            packages = subprocess.run([str(interpreter), "-c", "import importlib.metadata as m,json; print(json.dumps({d.metadata['Name']:d.version for d in m.distributions() if any(k in d.metadata['Name'].lower() for k in ['hindsight','pg0','pgvector','alembic'])}))"],
                                      capture_output=True, text=True, timeout=15)
            if packages.returncode == 0:
                result["daemon_packages"] = json.loads(packages.stdout)
                defaults = subprocess.run([str(interpreter), "-c", "import ast,importlib.metadata as m,json; p=m.distribution('hindsight-api-slim').locate_file('hindsight_api/config.py'); t=ast.parse(p.read_text()); names={'DEFAULT_EMBEDDINGS_PROVIDER','DEFAULT_EMBEDDINGS_LOCAL_MODEL','DEFAULT_EMBEDDING_DIMENSION'}; print(json.dumps({n.targets[0].id:n.value.value for n in t.body if isinstance(n,ast.Assign) and len(n.targets)==1 and isinstance(n.targets[0],ast.Name) and n.targets[0].id in names and isinstance(n.value,ast.Constant)}))"],
                                          capture_output=True, text=True, timeout=15)
                if defaults.returncode == 0:
                    result["memory_model_defaults"] = json.loads(defaults.stdout)
        values = {key.decode(): value.decode() for key, value in environment.items()
                  if any(word in key.upper() for word in (b"POSTGRES", b"DATABASE", b"PGHOST", b"PGUSER", b"PGDATABASE", b"PGPASSWORD", b"PGPORT"))}
        result["daemon_database_env_keys"] = sorted(values)
        for name, value in values.items():
            if value.startswith(("postgresql://", "postgres://")):
                url = urlsplit(value)
                env = dict(os.environ, PGHOST=url.hostname or "/tmp", PGPORT=str(url.port or 5432),
                           PGUSER=unquote(url.username or "postgres"), PGDATABASE=unquote(url.path.lstrip("/") or "postgres"),
                           PGPASSWORD=unquote(url.password or ""), PGCONNECT_TIMEOUT="5")
                psql = home / ".pg0/installation/18.1.0/bin/psql"
                run = subprocess.run([str(psql), "--no-psqlrc", "--no-password", "-Atc",
                    "SELECT datname FROM pg_database WHERE NOT datistemplate"], env=env, capture_output=True, text=True, timeout=10)
                result["connection_probe"] = {"credential_source": "daemon_environment:" + name, "connected": run.returncode == 0}
                if run.returncode == 0:
                    result["connection_probe"].update(database_names=run.stdout.splitlines(), user=env["PGUSER"], database=env["PGDATABASE"], host=env["PGHOST"], port=env["PGPORT"])
                    schema = subprocess.run([str(psql), "--no-psqlrc", "--no-password", "-Atc",
                        "SELECT table_schema || '.' || table_name FROM information_schema.tables WHERE table_name ILIKE '%migration%' OR table_name ILIKE '%version%'"],
                        env=env, capture_output=True, text=True, timeout=10)
                    if schema.returncode == 0:
                        result["schema_version_tables"] = schema.stdout.splitlines()
                    extensions = subprocess.run([str(psql), "--no-psqlrc", "--no-password", "-Atc",
                        "SELECT extname || ':' || extversion FROM pg_extension ORDER BY extname"], env=env, capture_output=True, text=True, timeout=10)
                    if extensions.returncode == 0:
                        result["postgres_extensions"] = extensions.stdout.splitlines()
                    if "public.alembic_version" in result.get("schema_version_tables", []):
                        revision = subprocess.run([str(psql), "--no-psqlrc", "--no-password", "-Atc", "SELECT version_num FROM public.alembic_version"],
                            env=env, capture_output=True, text=True, timeout=10)
                        if revision.returncode == 0:
                            result["public_alembic_revision"] = revision.stdout.splitlines()
                    break
    except (OSError, ValueError, subprocess.TimeoutExpired):
        result["daemon_probe_error"] = "unavailable"
    return result


if __name__ == "__main__":
    print(json.dumps(probe(), indent=2))
