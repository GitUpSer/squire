import os
import socket
import subprocess
import sys
import time
import httpx
import pytest


def test_real_ipv4_and_ipv6_health():
    if not socket.has_dualstack_ipv6():
        pytest.skip("dual-stack listener unavailable on test host")
    with socket.create_server(("::", 0),family=socket.AF_INET6,dualstack_ipv6=True) as reservation:
        port=reservation.getsockname()[1]
    process=subprocess.Popen([sys.executable,"-m","messaging.server"],
        env=dict(os.environ,PORT=str(port)),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        with httpx.Client(trust_env=False,timeout=1) as client:
            for _ in range(50):
                if process.poll() is not None:
                    pytest.fail("dual-stack API exited before readiness")
                try:
                    if client.get(f"http://127.0.0.1:{port}/readyz").status_code==200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(.1)
            else:
                pytest.fail("IPv4 health probe never became ready")
            assert client.get(f"http://[::1]:{port}/readyz").status_code==200
    finally:
        process.terminate()
        process.wait(timeout=10)
