"""Real CLI + Prisma + native Anthropic HTTP smoke; synthetic local provider only.

Run with the pinned LiteLLM environment after `prisma generate`. Each run creates
an isolated PostgreSQL schema. Never point PROXY_SMOKE_DATABASE_URL at production.
"""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import ExitStack, contextmanager

ROOT = Path(__file__).resolve().parents[1]
DATABASE = os.environ.get('PROXY_SMOKE_DATABASE_URL', 'postgresql://squire_test@127.0.0.1:55432/squire_integration_test')
if not any(host in DATABASE for host in ['@127.0.0.1:', '@localhost:']):
    raise SystemExit('Smoke database must be local')
SCHEMA = 'proxy_smoke_' + uuid.uuid4().hex[:12]
ARTIFACTS = Path(os.environ.get('PROXY_SMOKE_ARTIFACTS', '/tmp/squire-proxy-smoke'))
ARTIFACTS.mkdir(exist_ok=True, parents=True)
MASTER = 'sk-synthetic-smoke-master-0000000000000000'
requests_seen = []


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['content-length'])))
        requests_seen.append(body)
        # Inflated synthetic usage crosses the $10 cutoff without buying tokens.
        payload = {'id': 'msg_' + uuid.uuid4().hex, 'type':'message','role':'assistant',
            'model':body['model'],'content':[{'type':'text','text':'synthetic-response-only'}],
            'stop_reason':'end_turn','stop_sequence':None,
            'usage':{'input_tokens':100000,'output_tokens':50000,
                'cache_read_input_tokens':200000,'cache_creation_input_tokens':1000000,
                'cache_creation':{'ephemeral_5m_input_tokens':600000,'ephemeral_1h_input_tokens':400000}}}
        encoded=json.dumps(payload).encode()
        self.send_response(200)
        self.send_header('content-type','application/json')
        self.send_header('content-length',str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def wait_for(fn, seconds=120, process=None):
    deadline=time.monotonic()+seconds
    last=None
    while time.monotonic()<deadline:
        # Child failure is terminal, not another transient readiness response.
        if process is not None and process.poll() is not None:
            raise RuntimeError(f'proxy exited with status {process.returncode}; inspect proxy.log')
        try:
            result=fn()
            if result: return result
        except Exception as exc:
            last=exc
        time.sleep(.4)
    raise AssertionError(f'timeout waiting for smoke stage; {last}')


def stop_process(process, timeout=20):
    # The CLI can exit before cleanup. Still stop any descendants in its group,
    # and never replace the original test failure with ProcessLookupError.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def proxy_child(fd, config_path):
    """Use a continuously reserved socket without changing production code.

    The real CLI still runs migrations and the production dual-stack launcher.
    Only its socket factory receives our already-bound, inherited listener.
    The separate listener test exercises the production bind over IPv4/IPv6.
    """
    import serve
    listener = socket.socket(fileno=fd)
    original = serve.socket.create_server
    def reserved(address, **kwargs):
        if address == ('::', listener.getsockname()[1]):
            assert kwargs.get('family') == socket.AF_INET6 and kwargs.get('dualstack_ipv6') is True
            return listener
        return original(address, **kwargs)
    serve.socket.create_server = reserved
    sys.argv = [str(ROOT/'serve.py'), '--config', config_path]
    serve.main()


@contextmanager
def running_proxy():
    import yaml

    cfg=yaml.safe_load((ROOT/'config.yaml').read_text())
    with ExitStack() as resources:
        provider=resources.enter_context(ThreadingHTTPServer(('127.0.0.1',0),Provider))
        threading.Thread(target=provider.serve_forever,daemon=True).start()
        resources.callback(provider.shutdown)
        listener=resources.enter_context(socket.create_server(('::',0),family=socket.AF_INET6,dualstack_ipv6=True))
        port=listener.getsockname()[1]
        for item in cfg['model_list']:
            item['litellm_params']['api_key']='synthetic-provider-key'
            item['litellm_params']['api_base']=f'http://127.0.0.1:{provider.server_port}'
        cfg['general_settings']['proxy_batch_write_at']=1
        config_path=ARTIFACTS/'config.yaml'
        config_path.write_text(yaml.safe_dump(cfg))
        env=dict(os.environ, DATABASE_URL=DATABASE+'?schema='+SCHEMA,
            LITELLM_MASTER_KEY=MASTER,ANTHROPIC_API_KEY='synthetic-provider-key',
            PORT=str(port),LITELLM_LOCAL_MODEL_COST_MAP='True',
            NO_PROXY='*',no_proxy='*',PRISMA_BINARY_CACHE_DIR='/tmp/squire-prisma-cache',
            PYTHONPATH=str(ROOT),PROXY_BATCH_POLLING_ENABLED='false')
        env['PATH']=str(Path(sys.executable).parent)+':'+env['PATH']
        output=resources.enter_context(open(ARTIFACTS/'proxy.log','w'))
        process=subprocess.Popen([sys.executable,__file__,'--proxy-child',str(listener.fileno()),str(config_path)],
            env=env,stdout=output,stderr=subprocess.STDOUT,start_new_session=True,pass_fds=(listener.fileno(),))
        resources.callback(stop_process,process)
        listener.close()  # Child owns the reservation continuously across migrations.
        yield process, f'http://127.0.0.1:{port}'


def main():
    import httpx
    import psycopg
    from psycopg import sql

    with psycopg.connect(DATABASE, autocommit=True) as db:
        db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(SCHEMA)))
    with running_proxy() as (process, base_url):
        with httpx.Client(base_url=base_url,timeout=30,trust_env=False) as http:
            wait_for(lambda: http.get('/health/readiness',timeout=2).status_code==200,180,process)
            admin={'Authorization':'Bearer '+MASTER}
            def post(path, body):
                result=http.post(path,json=body,headers=admin)
                assert result.status_code==200, (path,result.status_code,result.text[:1000])
                return result.json()
            team=post('/team/new',{'team_alias':'synthetic-smoke','max_budget':10,'models':['anthropic:claude-sonnet-5']})['team_id']
            keys=[post('/key/generate',{'team_id':team,'key_alias':scope,'models':['anthropic:claude-sonnet-5'],'max_budget':10,'max_parallel_requests':1})['key'] for scope in ['private','group']]
            with psycopg.connect(DATABASE,autocommit=True) as db:
                db.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(SCHEMA)))
                def spend():
                    row=db.execute('SELECT spend FROM "LiteLLM_TeamTable" WHERE team_id=%s',(team,)).fetchone()
                    return row[0] if row else 0
                for index,key in enumerate([keys[0],keys[1],keys[0]],1):
                    response=http.post('/v1/messages',headers={'x-api-key':key,'anthropic-version':'2023-06-01'},json={'model':'anthropic:claude-sonnet-5','max_tokens':64000,'messages':[{'role':'user','content':'synthetic-private-prompt-never-persist'}]})
                    assert response.status_code==200,(response.status_code,response.text[:1000])
                    expected=3.84*index
                    wait_for(lambda: abs(spend()-expected)<.000001,60,process)
                    assert requests_seen[-1]['max_tokens']==8192,requests_seen[-1]
                count=len(requests_seen)
                denied=http.post('/v1/messages',headers={'x-api-key':keys[1],'anthropic-version':'2023-06-01'},json={'model':'anthropic:claude-sonnet-5','max_tokens':100,'messages':[{'role':'user','content':'denied'}]})
                assert denied.status_code in (400,401,402,403,429),(denied.status_code,denied.text[:1000])
                assert 'budget' in denied.text.lower(),denied.text
                assert len(requests_seen)==count==3
                rows=wait_for(lambda: db.execute('SELECT spend,team_id,messages,response FROM "LiteLLM_SpendLogs" WHERE team_id=%s',(team,)).fetchall(),60,process)
                assert len(rows)==3,rows
                assert abs(sum(row[0] for row in rows)-11.52)<.000001
                assert all('synthetic-private-prompt' not in json.dumps(row[2:]) and 'synthetic-response-only' not in json.dumps(row[2:]) for row in rows)
                report={'status':'passed','schema':SCHEMA,'native_anthropic_requests':3,'funded_provider_calls':0,'private_and_group_share_team':True,'team_spend':spend(),'expected_spend':11.52,'cache_priced':True,'budget_denial_status':denied.status_code,'persisted_prompt_bodies':False,'output_cap':8192}
                (ARTIFACTS/'report.json').write_text(json.dumps(report,indent=2)+'\n')
                print(json.dumps(report))
if __name__=='__main__':
    if sys.argv[1:2] == ['--proxy-child']:
        proxy_child(int(sys.argv[2]),sys.argv[3])
    else:
        main()
