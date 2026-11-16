"""Real dual-stack shim -> durable inbox -> real Hermes adapter -> scoped HTTP egress."""
import asyncio
import http.client
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import pytest
from test_runtime import event

@pytest.mark.asyncio
async def test_real_http_pipeline_replay_and_restart(hermes,scope,monkeypatch):
    deliveries=[]
    class BrokerHandler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def respond(self,value):
            encoded=json.dumps(value).encode();self.send_response(200);self.send_header("Content-Length",str(len(encoded)));self.end_headers();self.wfile.write(encoded)
        def do_GET(self):self.respond({"approvals":[]})
        def do_POST(self):
            assert self.headers["Authorization"]=="Bearer "+scope.egress_token
            payload=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.path=="/v1/outbound"
            deliveries.append(payload);self.respond({"operation_id":"broker-"+payload["response_key"]})
    broker=ThreadingHTTPServer(("127.0.0.1",0),BrokerHandler)
    thread=threading.Thread(target=broker.serve_forever,daemon=True);thread.start()
    monkeypatch.setenv("SQUIRE_MESSAGING_URL",f"http://127.0.0.1:{broker.server_port}")
    listener=socket.create_server(("::",0),family=socket.AF_INET6,dualstack_ipv6=True)
    port=listener.getsockname()[1];listener.close()
    shim=Path(__file__).resolve().parents[2]/"bin"/"squire-webhook-shim.py"
    child=subprocess.Popen([sys.executable,str(shim)],env={**os.environ,"PORT":str(port)},stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    from squire_overlay.adapter import SquireAdapter
    from gateway.config import PlatformConfig
    adapter=SquireAdapter(PlatformConfig());seen=[]
    async def handler(message):seen.append(message.text);return "Reply through the real Squire adapter"
    adapter.set_message_handler(handler)
    def post(host,body,token):
        connection=http.client.HTTPConnection(host,port,timeout=5)
        connection.request("POST","/events",json.dumps(body),headers={"Content-Type":"application/json","Authorization":"Bearer "+token})
        response=connection.getresponse();result=(response.status,json.loads(response.read()));connection.close();return result
    try:
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1",port),timeout=.1):break
            except OSError:await asyncio.sleep(.05)
        body={"events":[event(scope)]}
        assert post("127.0.0.1",body,scope.egress_token)[0]==401
        assert post("127.0.0.1",body,scope.ingress_token)[0]==202
        assert post("::1",body,scope.ingress_token)[0]==202
        assert post("::1",{"events":[{**event(scope,"foreign"),"conversation_id":"another"}]},scope.ingress_token)[0]==422
        await adapter.connect()
        for _ in range(150):
            if deliveries:break
            await asyncio.sleep(.05)
        assert len(deliveries)==1
        assert deliveries[0]["conversation_id"]==scope.conversation_id
        assert deliveries[0]["cause_event_id"]=="one"
        assert seen==["Hello"]
        await adapter.disconnect()
        # New input accepted while gateway is offline is committed, not lost.
        assert post("::1",{"events":[event(scope,"two")]},scope.ingress_token)[0]==202
        assert post("127.0.0.1",body,scope.ingress_token)[0]==202
        restarted=SquireAdapter(PlatformConfig());restarted.set_message_handler(handler)
        await restarted.connect()
        try:
            for _ in range(150):
                if len(deliveries)==2:break
                await asyncio.sleep(.05)
            assert len(deliveries)==2
            assert seen==["Hello","Hello"]
        finally:await restarted.disconnect()
    finally:
        if adapter._running:await adapter.disconnect()
        child.terminate();child.communicate(timeout=10)
        broker.shutdown();broker.server_close()
