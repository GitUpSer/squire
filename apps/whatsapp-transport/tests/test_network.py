"""Real listener regression; opt in where loopback socket binding is permitted."""
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest


@pytest.mark.skipif(os.environ.get("TRANSPORT_NETWORK_TEST") != "1", reason="requires IPv4/IPv6 loopback")
def test_health_on_ipv4_and_ipv6(tmp_path):
    with socket.create_server(("::", 0), family=socket.AF_INET6, dualstack_ipv6=True) as reserve:
        port = reserve.getsockname()[1]
    env = dict(os.environ, PORT=str(port), TRANSPORT_HOST="::", TRANSPORT_DATA_DIR=str(tmp_path),
               TRANSPORT_PROVIDER_ACCOUNT_ID="test", TRANSPORT_BROKER_TOKEN="b" * 32,
               TRANSPORT_OPERATOR_TOKEN="o" * 32, MESSAGING_TRANSPORT_TOKEN="m" * 32,
               TRANSPORT_MESSAGING_URL="http://127.0.0.1:9", TRANSPORT_REQUIRE_MOUNT="false",
               TRANSPORT_SYNC_ENABLED="false", TRANSPORT_OUTBOUND_ENABLED="false")
    process = subprocess.Popen([sys.executable, "-c", "from whatsapp_transport.main import main; main()"],
                               env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with httpx.Client(trust_env=False, timeout=1) as client:
            deadline = time.monotonic() + 15
            while True:
                try:
                    assert client.get(f"http://127.0.0.1:{port}/healthz").status_code == 200
                    break
                except httpx.ConnectError:
                    assert process.poll() is None and time.monotonic() < deadline
                    time.sleep(0.1)
            assert client.get(f"http://[::1]:{port}/healthz").status_code == 200
            assert client.get(f"http://127.0.0.1:{port}/readyz").status_code == 503
    finally:
        process.terminate()
        process.wait(timeout=15)
