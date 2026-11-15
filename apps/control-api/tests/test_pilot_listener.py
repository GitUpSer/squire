"""Exercise the actual console listener on IPv4 and IPv6."""
import http.client
import os
from pathlib import Path
import socket
import subprocess
import sys
import time


def test_console_listener_is_dual_stack(tmp_path):
    listener=socket.create_server(("::",0),family=socket.AF_INET6,dualstack_ipv6=True)
    port=listener.getsockname()[1];listener.close()
    env={**os.environ,"PORT":str(port),"DATABASE_URL":"sqlite:///"+str(tmp_path/"listener.db"),"PILOT_ENABLED":"false","PROVISION_WORKER_ENABLED":"false","PILOT_WORKER_ENABLED":"false","PYTHONPATH":str(Path(__file__).resolve().parents[1]/"src")}
    process=subprocess.Popen([sys.executable,"-c","from control_api.main import run; run()"],env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    try:
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1",port),timeout=.1):break
            except OSError:time.sleep(.05)
        for host in ("127.0.0.1","::1"):
            client=http.client.HTTPConnection(host,port,timeout=5)
            client.request("GET","/healthz")
            response=client.getresponse();assert response.status==200;response.read();client.close()
    finally:
        process.terminate();process.communicate(timeout=10)
