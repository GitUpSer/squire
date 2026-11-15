"""Image build gate: real Camofox browser loads a local synthetic page."""
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.request import Request,urlopen

class Page(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):
        body=b'<html><head><title>Squire browser smoke</title></head><body>local synthetic browser proof</body></html>'
        self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)


def request(path,payload=None):
    req=Request('http://127.0.0.1:9377'+path,data=json.dumps(payload).encode() if payload else None,headers={'Content-Type':'application/json'})
    with urlopen(req,timeout=45) as response:return json.load(response)


def main():
    page=ThreadingHTTPServer(('127.0.0.1',9378),Page)
    threading.Thread(target=page.serve_forever,daemon=True).start()
    with tempfile.TemporaryDirectory(prefix='squire-browser-smoke-') as directory:
        env=dict(os.environ,CAMOFOX_PORT='9377',ENABLE_VNC='0',CAMOFOX_CRASH_REPORT_ENABLED='false',SENTRY_DSN='',
                 CAMOFOX_PROFILE_DIR=directory+'/profiles',CAMOFOX_COOKIES_DIR=directory+'/cookies',CAMOFOX_TRACES_DIR=directory+'/traces')
        with open(directory+'/server.log','w+') as log:
            proc=subprocess.Popen(['node','server.js'],cwd='/opt/squire/camofox',env=env,stdout=log,stderr=log,start_new_session=True)
            try:
                deadline=time.monotonic()+120
                while time.monotonic()<deadline:
                    try:
                        if request('/health').get('ok'):break
                    except Exception:pass
                    if proc.poll() is not None:raise RuntimeError('Camofox exited')
                    time.sleep(.5)
                else:raise RuntimeError('Camofox did not start')
                tab=request('/tabs',{'userId':'squire-build-smoke','sessionKey':'test','url':'http://127.0.0.1:9378/'})
                assert tab.get('tabId'),tab
                snapshot=request('/tabs/'+tab['tabId']+'/snapshot?userId=squire-build-smoke')
                assert 'local synthetic browser proof' in json.dumps(snapshot),snapshot
                assert request('/health')['browserConnected']
                print('Camofox 1.11.2 / Firefox 135 beta.24 actual browser smoke passed')
            except Exception:
                log.flush();log.seek(0);print(log.read()[-6000:])  # synthetic build only
                raise
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid,signal.SIGTERM)
                    try:proc.wait(15)
                    except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);proc.wait()
    page.shutdown()

if __name__=='__main__':main()
