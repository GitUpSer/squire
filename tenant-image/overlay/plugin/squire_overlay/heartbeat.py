"""Minimal pilot readiness: same storage/adapter boundary the broker relies on."""
import json
import signal
import threading
import time
import urllib.error
import urllib.request
from .config import RuntimeConfig
from .store import Store


def run(once=False):
    scope=RuntimeConfig.from_env();store=Store(scope.database);stop=threading.Event()
    def emit():
        status=store.status(scope.generation)
        payload={key:status[key] for key in ("ready","model_ready","deployment_generation")}
        request=urllib.request.Request(scope.control_url+"/v1/runtime/"+scope.runtime_id+"/heartbeat",method="POST",data=json.dumps(payload).encode(),headers={"Authorization":"Bearer "+scope.control_token,"Content-Type":"application/json"})
        try:
            with urllib.request.urlopen(request,timeout=15) as response:ok=200<=response.status<300
        except (OSError,urllib.error.URLError):ok=False
        return ok,payload
    if once:return 0 if emit()[0] else 1
    signal.signal(signal.SIGTERM,lambda *_:stop.set());signal.signal(signal.SIGINT,lambda *_:stop.set())
    previous=None;last_sent=0
    import os
    interval=max(30,int(os.environ.get("SQUIRE_HEARTBEAT_INTERVAL","1800")))
    while not stop.wait(2):
        status=store.status(scope.generation)
        snapshot=(status["ready"],status["model_ready"])
        if snapshot!=previous or time.monotonic()-last_sent>=interval:
            ok,payload=emit()
            if ok:previous=snapshot;last_sent=time.monotonic()
    return 0
