#!/usr/bin/env python3
"""Start real services and browser against disposable loopback fixtures.

Run with a Python containing psycopg/httpx. Requires the two app environments,
Caddy and Playwright. All secrets below are public test-only values.
"""
import json
import os
from pathlib import Path
import subprocess
import signal
import sys
import tempfile
import time
from urllib.parse import quote
import httpx
import psycopg

ROOT = Path(__file__).resolve().parents[2]
DATABASE = os.environ.get("PILOT_TEST_DATABASE_URL", "postgresql://squire_test@127.0.0.1:55432/squire_integration_test")
if "_test" not in DATABASE:
    raise SystemExit("Disposable database URL must contain _test")
ARTIFACTS = Path(os.environ.get("PILOT_TEST_ARTIFACTS", "/tmp/squire-pilot-acceptance"))
ARTIFACTS.mkdir(parents=True, exist_ok=True)
CONTROL_PYTHON = os.environ.get("CONTROL_PYTHON", str(ROOT / "apps/control-api/.venv/bin/python"))
MESSAGING_PYTHON = os.environ.get("MESSAGING_PYTHON", "/tmp/squire-messaging-venv/bin/python")
CADDY = os.environ.get("CADDY_BIN", "/tmp/squire-caddy/caddy")
PROCESSES, LOGS = [], []


def start(name, command, env):
    log = (ARTIFACTS / (name+".log")).open("w")
    LOGS.append(log)
    process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    PROCESSES.append((name,process))
    return process


def wait_http(url, timeout=30):
    end = time.monotonic()+timeout
    with httpx.Client(trust_env=False, timeout=2) as client:
        while time.monotonic() < end:
            for name, process in PROCESSES:
                if process.poll() is not None:
                    raise RuntimeError(name+" exited; inspect "+str(ARTIFACTS/(name+".log")))
            try:
                if client.get(url).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(.2)
    raise RuntimeError("service did not become ready: "+url)


def main():
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    with psycopg.connect(DATABASE, autocommit=True) as conn:
        # Fixed test-only schema names, independent of production databases.
        conn.execute("DROP SCHEMA IF EXISTS pilot_control CASCADE")
        conn.execute("DROP SCHEMA IF EXISTS pilot_messaging CASCADE")
        conn.execute("CREATE SCHEMA pilot_control")
        conn.execute("CREATE SCHEMA pilot_messaging")
    with tempfile.TemporaryDirectory(prefix="squire-pilot-fixture-") as fixture:
        common = dict(os.environ, PYTHONPATH=str(ROOT/"tests/pilot"),
            SQUIRE_PILOT_FIXTURE="1", PILOT_FIXTURE_DIR=fixture,
            NO_PROXY="*", no_proxy="*", PYTHONUNBUFFERED="1")
        control = dict(common, DATABASE_URL=DATABASE+"?options="+quote("-csearch_path=pilot_control"),
            INTERNAL_API_TOKEN="operator-integration-test-token", PILOT_ENABLED="true",
            PILOT_ADMISSION_OPEN="true", PILOT_SECRET="integration-only-secret-"+"s"*40,
            PILOT_WEB_ORIGIN="http://127.0.0.1:58740", PILOT_COOKIE_SECURE="false",
            CONTROL_API_URL="http://127.0.0.1:58741", CONTROL_API_INTERNAL_URL="http://127.0.0.1:58741",
            MESSAGING_BASE_URL="http://127.0.0.1:58742", MESSAGING_SERVICE_TOKEN="c"*40,
            CONTROL_MESSAGING_SERVICE_TOKEN="b"*40,
            RESEND_API_KEY="fixture-email",RESEND_FROM="pilot@fixture.invalid",RESEND_API_URL="http://127.0.0.1:58744",
            RAILWAY_API_TOKEN="fixture-railway",RAILWAY_GRAPHQL_URL="http://127.0.0.1:58744/graphql",
            RAILWAY_PROJECT_ID="fixture-project",RAILWAY_ENVIRONMENT_ID="fixture-environment",
            RAILWAY_PRIVATE_DOMAIN_SUFFIX="pilot.test",TENANT_PORT="58744",
            RAILWAY_VOLUME_PROBE_DELAY_SECONDS="0",PROVISION_BACKOFF_BASE_SECONDS="1",
            PROVISION_WORKER_ENABLED="true",PROVISION_WORKER_INTERVAL_SECONDS="0.5",PILOT_WORKER_ENABLED="true",
            LITELLM_BASE_URL="http://127.0.0.1:58744",LITELLM_MASTER_KEY="fixture-master",
            TELEGRAM_API_BASE_URL="http://127.0.0.1:58744/disabled-telegram")
        messaging = dict(common, MESSAGING_DATABASE_URL=DATABASE+"?options="+quote("-csearch_path=pilot_messaging"),
            MESSAGING_ENCRYPTION_KEY="e"*40,MESSAGING_HMAC_KEY="h"*40,
            MESSAGING_CONTROL_TOKEN="c"*40,MESSAGING_TRANSPORT_TOKEN="t"*40,MESSAGING_OPERATOR_TOKEN="o"*40,
            MESSAGING_CONTROL_URL="http://127.0.0.1:58741",MESSAGING_CONTROL_CALLBACK_TOKEN="b"*40,
            MESSAGING_TRANSPORT_URL="http://127.0.0.1:58744",MESSAGING_TRANSPORT_BROKER_TOKEN="p"*40,
            MESSAGING_RUNTIME_HOST_SUFFIX=".pilot.test",MESSAGING_OUTBOUND_ENABLED="true",
            MESSAGING_SEND_SPACING_SECONDS="0.05")
        subprocess.run([MESSAGING_PYTHON,"-c","from messaging.db import migrate; migrate()"],cwd=ROOT,env=messaging,check=True)
        config = (ROOT/"apps/pilot-web/Caddyfile").read_text().replace("root * /srv", "root * "+str(ROOT/"apps/pilot-web/site"))
        caddyfile = Path(fixture)/"Caddyfile"
        caddyfile.write_text(config)
        try:
            start("fixtures",[CONTROL_PYTHON,"-m","uvicorn","fixtures:app","--host","127.0.0.1","--port","58744","--no-access-log"],common)
            wait_http("http://127.0.0.1:58744/healthz")
            start("messaging",[MESSAGING_PYTHON,"-m","uvicorn","messaging.main:app","--host","127.0.0.1","--port","58742","--no-access-log"],messaging)
            wait_http("http://127.0.0.1:58742/readyz")
            start("control",[CONTROL_PYTHON,"-m","uvicorn","control_api.main:app","--host","127.0.0.1","--port","58741","--no-access-log"],control)
            wait_http("http://127.0.0.1:58741/healthz")
            start("messaging-worker",[MESSAGING_PYTHON,"-m","messaging.worker"],messaging)
            start("web",[CADDY,"run","--config",str(caddyfile),"--adapter","caddyfile"],dict(common,PORT="58740",CONTROL_API_UPSTREAM="127.0.0.1:58741"))
            wait_http("http://127.0.0.1:58740")
            with httpx.Client(trust_env=False) as client:
                result=client.post("http://127.0.0.1:58742/v1/operator/provider",
                    headers={"Authorization":"Bearer "+"o"*40},json={"paused":False})
                result.raise_for_status()
            browser_env=dict(common,PILOT_TEST_ARTIFACTS=str(ARTIFACTS))
            result=subprocess.run(["node",str(ROOT/"tests/pilot/browser.cjs")],env=browser_env,cwd=ROOT,timeout=300)
            if result.returncode:
                raise RuntimeError("browser acceptance failed; inspect artifacts in "+str(ARTIFACTS))
            print("Pilot browser acceptance passed. Artifacts: "+str(ARTIFACTS))
        finally:
            for _, process in reversed(PROCESSES):
                process.terminate()
            for _, process in reversed(PROCESSES):
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
            for log in LOGS:
                log.close()


if __name__ == "__main__":
    main()
