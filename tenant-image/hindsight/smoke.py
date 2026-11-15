"""Build gate: real embedded PostgreSQL + API + Hermes-interpreter HTTP client.

Only embeddings are synthetic (384 dimensions); no external model is called.
This verifies schema startup and protocol compatibility, not recall quality.
"""
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


def check():
    from importlib.metadata import version
    from hindsight_client import Hindsight
    assert version("hindsight-client") == "0.10.3"
    with tempfile.TemporaryDirectory(prefix="squire-memory-build-") as directory:
        env = dict(os.environ, HOME=directory, HERMES_HOME=directory,
                   XDG_CACHE_HOME=directory + "/cache",
                   HINDSIGHT_API_WORKER_ENABLED="false",
                   HINDSIGHT_API_LLM_PROVIDER="none",
                   HINDSIGHT_API_RERANKER_PROVIDER="rrf",
                   HINDSIGHT_API_SKIP_LLM_VERIFICATION="true")
        with open(Path(directory) / "api.log", "w+") as log:
            proc = subprocess.Popen(["/opt/squire/hindsight-venv/bin/python", __file__, "serve"],
                                    env=env, stdout=log, stderr=log, start_new_session=True)
            client = None
            try:
                deadline = time.monotonic() + 240
                while time.monotonic() < deadline:
                    try:
                        with urlopen("http://127.0.0.1:19177/health", timeout=2) as response:
                            if json.load(response).get("status") == "healthy": break
                    except Exception:
                        pass
                    if proc.poll() is not None: raise RuntimeError("Hindsight startup failed")
                    time.sleep(.5)
                else: raise RuntimeError("Hindsight health deadline exceeded")
                client = Hindsight(base_url="http://127.0.0.1:19177", timeout=20)
                client.create_bank(bank_id="squire-image-smoke", name="Synthetic build gate")
                recalled = client.recall(bank_id="squire-image-smoke", query="synthetic empty bank", types=["observation"])
                assert recalled.results == [], "new synthetic bank must be empty"
                client.delete_bank("squire-image-smoke")
                api_version, pg_version = expected_versions()
                print(f"Hindsight {api_version} / pg0 {pg_version} / Hermes client 0.10.3 health and HTTP bank/recall gate passed")
            except Exception:
                log.flush(); log.seek(0); print(log.read()[-10000:])
                raise
            finally:
                if client is not None: client.close()
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGTERM)
                    try: proc.wait(20)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL); proc.wait()


if __name__ == "__main__":
    serve() if sys.argv[1:] == ["serve"] else check()
