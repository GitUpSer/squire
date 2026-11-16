"""Build gate: real embedded PostgreSQL + API + Hermes-interpreter HTTP client.

Only embeddings are synthetic (384 dimensions); no external model is called.
This verifies schema startup and protocol compatibility, not recall quality.
"""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen


def expected_versions():
    return {"founder": ("0.5.2", "0.12.3"), "standard": ("0.9.0", "0.15.1")}[os.environ["SQUIRE_HINDSIGHT_VARIANT"]]


def serve():
    import uvicorn
    from importlib.metadata import version
    from hindsight_api import MemoryEngine
    from hindsight_api.api import create_app
    from hindsight_api.engine.embeddings import Embeddings
    from hindsight_api.engine.cross_encoder import RRFPassthroughCrossEncoder

    api_version, pg_version = expected_versions()
    assert version("hindsight-api-slim") == api_version
    assert version("pg0-embedded") == pg_version
    assert version("sqlalchemy") == "2.0.52"
    from sqlalchemy import create_engine
    # 0.5.2 declares psycopg2 but omitted the <2.1 constraint added by 0.9.
    # SQLAlchemy 2.1 silently switches the plain PostgreSQL URL to psycopg3.
    driver_check = create_engine("postgresql://synthetic@127.0.0.1/unused")
    assert driver_check.dialect.driver == "psycopg2"
    driver_check.dispose()

    class SyntheticEmbeddings(Embeddings):
        @property
        def provider_name(self): return "build-synthetic"
        @property
        def dimension(self): return 384
        async def initialize(self): pass
        def encode(self, texts): return [[1.0] + [0.0] * 383 for _ in texts]

    memory = MemoryEngine(db_url="pg0://squire-image-gate:55439",
                          memory_llm_provider="none",
                          embeddings=SyntheticEmbeddings(),
                          cross_encoder=RRFPassthroughCrossEncoder(),
                          skip_llm_verification=True,
                          pool_min_size=1, pool_max_size=3)
    uvicorn.run(create_app(memory), host="127.0.0.1", port=19177,
                access_log=False, log_level="warning")


def wait_healthy(proc):
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        try:
            with urlopen("http://127.0.0.1:19177/health", timeout=2) as response:
                if json.load(response).get("status") == "healthy":
                    return
        except Exception:
            pass
        if proc.poll() is not None:
            raise RuntimeError("Hindsight startup failed")
        time.sleep(.5)
    raise RuntimeError("Hindsight health deadline exceeded")


def stop_api(proc):
    if proc is None or proc.poll() is not None:
        return
    os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(45)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        raise RuntimeError("Hindsight did not stop cleanly")


def bank_snapshot():
    # Unlike the profile GET endpoint, listing cannot silently create the bank.
    with urlopen("http://127.0.0.1:19177/v1/default/banks", timeout=10) as response:
        banks = json.load(response)["banks"]
    found = [bank for bank in banks if bank["bank_id"] == "squire-image-smoke"]
    assert len(found) == 1 and found[0]["name"] == "Synthetic build gate"
    return found[0]


def cluster_identity(metadata):
    installation = Path(metadata["installation_dir"])
    binaries = list(installation.rglob("pg_controldata"))
    assert len(binaries) == 1
    result = subprocess.run([str(binaries[0]), metadata["data_dir"]],
                            capture_output=True, text=True, check=True,
                            env=dict(os.environ, LC_ALL="C"))
    fields = dict(line.split(":", 1) for line in result.stdout.splitlines() if ":" in line)
    return fields["Database system identifier"].strip(), fields["Database cluster state"].strip()


def data_hashes(directory):
    result = {}
    for path in sorted(Path(directory).rglob("*")):
        if path.is_file():
            with path.open("rb") as stream:
                result[str(path.relative_to(directory))] = hashlib.file_digest(stream, "sha256").hexdigest()
    return result


def live_guard_checks(env):
    # A competing launcher must fail while the descriptor remains held across
    # the guard's exec into the real API process.
    command = ["/opt/squire/hindsight-venv/bin/python", "/opt/squire/bin/squire-pg0-prestart.py", "/bin/true"]
    conflict = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
    assert conflict.returncode != 0 and "another_hindsight_instance_holds_startup_lock" in conflict.stderr
    # Directly validate the read-only live branch against actual pg0 argv and
    # postmaster.pid, rather than assuming the unit-test fixtures match it.
    code = r"""
import importlib.util,json,os
from pathlib import Path
spec=importlib.util.spec_from_file_location('guard','/opt/squire/bin/squire-pg0-prestart.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
d=Path(os.environ['HOME'])/'.pg0/instances/squire-image-gate'
r=json.loads((d/'instance.json').read_text())
m.live_cluster(r,d/'data',d/'data/postmaster.pid',Path(r['installation_dir']))
"""
    check = subprocess.run(["/opt/squire/hindsight-venv/bin/python", "-c", code], env=env,
                           capture_output=True, text=True, timeout=10)
    assert check.returncode == 0, "guard rejected the actual native PostgreSQL identity"


def upstream_rejects_recycled_pid(env):
    code = r"""
from pg0 import Pg0, Pg0AlreadyRunningError
try:
    Pg0(name='squire-image-gate',port=55439).start()
except Pg0AlreadyRunningError:
    pass
else:
    raise AssertionError('pinned_pg0_did_not_reproduce_live_pid_reuse_failure')
"""
    result = subprocess.run(["/opt/squire/hindsight-venv/bin/python", "-c", code], env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, "pinned pg0 failed to reproduce the recycled-PID defect"


def repair_stale_metadata(env, api_version, pg_version):
    # Invoke the exact installed guard under the memory interpreter. Both pinned
    # pg0 versions reject a recycled live PID in start(), although 0.15.1's info()
    # additionally checks health. Neither may kill the unrelated process.
    code = r"""
import fcntl, importlib.util, os
from pathlib import Path
spec = importlib.util.spec_from_file_location('pg0_prestart', '/opt/squire/bin/squire-pg0-prestart.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
directory = Path(os.environ['HOME'])/'.pg0/instances/squire-image-gate'
with (directory/'.squire-startup.lock').open('a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    module.repair(Path(os.environ['HOME']), os.environ['HINDSIGHT_API_DATABASE_URL'], os.environ['SQUIRE_GATE_PG_VERSION'], os.environ['SQUIRE_GATE_API_VERSION'])
"""
    result = subprocess.run(["/opt/squire/hindsight-venv/bin/python", "-c", code],
                            env=dict(env, SQUIRE_GATE_PG_VERSION=pg_version,
                                     SQUIRE_GATE_API_VERSION=api_version),
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, "installed pg0 stale-metadata guard failed"


def check():
    from importlib.metadata import version
    from hindsight_client import Hindsight
    assert version("hindsight-client") == "0.10.3"
    api_version, pg_version = expected_versions()
    with tempfile.TemporaryDirectory(prefix="squire-memory-build-") as directory:
        env = dict(os.environ, HOME=directory, HERMES_HOME=directory,
                   XDG_CACHE_HOME=directory + "/cache",
                   HINDSIGHT_API_DATABASE_URL="pg0://squire-image-gate:55439",
                   HINDSIGHT_API_WORKER_ENABLED="false",
                   HINDSIGHT_API_LLM_PROVIDER="none",
                   HINDSIGHT_API_RERANKER_PROVIDER="rrf",
                   HINDSIGHT_API_SKIP_LLM_VERIFICATION="true")
        with open(Path(directory) / "api.log", "w+") as log:
            proc = None
            client = None
            unrelated = None
            try:
                proc = subprocess.Popen(["/opt/squire/hindsight-venv/bin/python", "/opt/squire/bin/squire-pg0-prestart.py",
                                         "/opt/squire/hindsight-venv/bin/python", __file__, "serve"],
                                        env=env, stdout=log, stderr=log, start_new_session=True)
                wait_healthy(proc)
                client = Hindsight(base_url="http://127.0.0.1:19177", timeout=20)
                client.create_bank(bank_id="squire-image-smoke", name="Synthetic build gate")
                recalled = client.recall(bank_id="squire-image-smoke", query="synthetic empty bank", types=["observation"])
                assert recalled.results == [], "new synthetic bank must be empty"
                live_guard_checks(env)
                original_bank = bank_snapshot()
                metadata_path = Path(directory) / ".pg0/instances/squire-image-gate/instance.json"
                metadata = json.loads(metadata_path.read_text())
                original_cluster, _ = cluster_identity(metadata)
                client.close()
                client = None
                stop_api(proc)
                proc = None
                assert not (Path(metadata["data_dir"]) / "postmaster.pid").exists()
                assert cluster_identity(metadata) == (original_cluster, "shut down")

                unrelated = subprocess.Popen(["sleep", "300"], env=env, start_new_session=True)
                metadata["pid"] = unrelated.pid
                metadata_path.write_text(json.dumps(metadata))
                metadata_path.chmod(0o600)
                poisoned_metadata = metadata_path.read_bytes()
                stopped_files = data_hashes(metadata["data_dir"])
                upstream_rejects_recycled_pid(env)
                assert unrelated.poll() is None and metadata_path.read_bytes() == poisoned_metadata
                assert data_hashes(metadata["data_dir"]) == stopped_files, "upstream failure changed cluster files"
                repair_stale_metadata(env, api_version, pg_version)
                assert unrelated.poll() is None, "guard signaled an unrelated live process"
                assert not metadata_path.exists(), "stale instance metadata was not archived"
                assert cluster_identity(metadata) == (original_cluster, "shut down"), "guard altered the database"
                assert data_hashes(metadata["data_dir"]) == stopped_files, "guard modified database or WAL files"

                proc = subprocess.Popen(["/opt/squire/hindsight-venv/bin/python", "/opt/squire/bin/squire-pg0-prestart.py",
                                         "/opt/squire/hindsight-venv/bin/python", __file__, "serve"],
                                        env=env, stdout=log, stderr=log, start_new_session=True)
                wait_healthy(proc)
                assert bank_snapshot() == original_bank, "existing bank was lost or rewritten after restart"
                assert cluster_identity(json.loads(metadata_path.read_text()))[0] == original_cluster
                client = Hindsight(base_url="http://127.0.0.1:19177", timeout=20)
                recalled = client.recall(bank_id="squire-image-smoke", query="synthetic empty bank after restart", types=["observation"])
                assert recalled.results == []
                assert unrelated.poll() is None, "restart signaled an unrelated live process"
                client.delete_bank("squire-image-smoke")
                print(f"Hindsight {api_version} / pg0 {pg_version} / Hermes client 0.10.3 health, bank/recall and live-PID-reuse restart durability gate passed")
            except Exception:
                log.flush()
                log.seek(0)
                print(log.read()[-10000:])
                raise
            finally:
                if client is not None:
                    client.close()
                try:
                    stop_api(proc)
                finally:
                    if unrelated is not None and unrelated.poll() is None:
                        unrelated.terminate()
                        unrelated.wait(10)


if __name__ == "__main__":
    serve() if sys.argv[1:] == ["serve"] else check()
