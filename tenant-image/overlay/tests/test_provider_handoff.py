"""Provider handoff proves the selected owner route before removing trial funding."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
import pytest

BIN=Path(__file__).resolve().parents[2]/'bin'
sys.path.insert(0,str(BIN))
import squire_connect as connect
from squire_overlay.store import Store
from squire_overlay.heartbeat import reconcile_provider


def ready(scope,provider='anthropic'):
    store=Store(scope.database);store.mark_runtime(scope.generation,True,True,provider)
    return store.status(scope.generation)


@pytest.mark.parametrize('failure',['switch','restart','route'])
def test_failed_handoff_never_celebrates_or_revokes(scope,monkeypatch,failure):
    calls=[]
    monkeypatch.setattr(connect,'switch_gateway_model',lambda *args:failure!='switch')
    monkeypatch.setattr(connect,'restart_gateway',lambda:failure!='restart')
    monkeypatch.setattr(connect,'notify_control_api',lambda *args:calls.append('revoke'))
    monkeypatch.setattr(connect,'notify_owner_connected',lambda *args:calls.append('celebrate'))
    ready(scope,None if failure=='route' else 'anthropic')
    assert not connect.run_connected_pipeline('anthropic')
    assert connect.connection_status()['state']=='error' and calls==[]
    assert reconcile_provider(scope,ready(scope)) and calls==[]


def test_lost_callback_reconciles_from_durable_status_after_restart(scope,monkeypatch):
    calls=[]
    monkeypatch.setattr(connect,'switch_gateway_model',lambda *args:True)
    monkeypatch.setattr(connect,'restart_gateway',lambda:True)
    monkeypatch.setattr(connect,'notify_owner_connected',lambda *args:calls.append('celebrate'))
    monkeypatch.setattr(connect,'notify_control_api',lambda provider:False)
    ready(scope)
    assert connect.run_connected_pipeline('anthropic')
    assert connect.connection_status()['state']=='connected'
    assert not reconcile_provider(scope,Store(scope.database).status(scope.generation))
    monkeypatch.setattr(connect,'notify_control_api',lambda provider:calls.append(provider) or True)
    assert reconcile_provider(scope,Store(scope.database).status(scope.generation))
    assert calls==['celebrate','anthropic']


def test_founder_preconnected_codex_needs_no_new_flow_and_group_skips(scope,monkeypatch):
    calls=[];monkeypatch.setattr(connect,'notify_control_api',lambda p:calls.append(p) or True)
    assert connect.connection_status()['state']=='none'
    assert reconcile_provider(scope,ready(scope,'chatgpt'))
    assert calls==['chatgpt'] and connect.connection_status()['state']=='none'
    assert reconcile_provider(SimpleNamespace(kind='group'),{'owner_provider':'chatgpt'})
    assert calls==['chatgpt']


def test_readiness_provider_requires_live_fresh_same_generation_and_migrates(scope,monkeypatch):
    scope.database.parent.mkdir()
    with sqlite3.connect(scope.database) as db:
        db.execute('CREATE TABLE runtime (id INTEGER PRIMARY KEY,pid INTEGER,generation INTEGER,ready INTEGER,model_ready INTEGER,updated REAL)')
        db.execute('INSERT INTO runtime VALUES(1,?,?,1,1,0)',(os.getpid(),scope.generation))
    store=Store(scope.database);assert store.status(scope.generation)['owner_provider'] is None
    ready(scope);assert store.status(scope.generation)['owner_provider']=='anthropic'
    assert store.status(scope.generation+1)['owner_provider'] is None
    with store.db() as db:db.execute('UPDATE runtime SET updated=0')
    assert store.status(scope.generation)['owner_provider'] is None
    ready(scope)
    monkeypatch.setattr(os,'kill',lambda *args:(_ for _ in ()).throw(ProcessLookupError()))
    assert store.status(scope.generation)['owner_provider'] is None


def test_missing_model_entry_cannot_succeed(scope):
    assert not connect.switch_gateway_model('openai')
    assert 'model:' not in (scope.home/'config.yaml').read_text()


def test_selected_credential_and_endpoint_must_match(scope,monkeypatch):
    from squire_overlay.adapter import resolved_owner_provider
    import hermes_cli.runtime_provider as resolver
    (scope.home/'config.yaml').write_text('model:\n  provider: anthropic\n  default: claude-sonnet-5\n')
    (scope.home/'.env').write_text('ANTHROPIC_API_KEY=owner-key\nANTHROPIC_BASE_URL=https://api.anthropic.com\n')
    effective={'provider':'anthropic','api_key':'owner-key','base_url':'https://api.anthropic.com'}
    monkeypatch.setattr(resolver,'resolve_runtime_provider',lambda:effective)
    assert resolved_owner_provider(scope)=='anthropic'
    for change in ({'api_key':'trial-key'},{'base_url':'http://proxy.railway.internal'},{'provider':'openai'}):
        original=effective.copy();effective.update(change);assert resolved_owner_provider(scope) is None;effective.clear();effective.update(original)
    (scope.home/'config.yaml').write_text('model:\n  provider: openai\n  default: gpt-4.1\n')
    assert resolved_owner_provider(scope) is None


@pytest.mark.parametrize('provider',['anthropic','openai','chatgpt'])
def test_actual_hermes_resolver_selects_owner_key_over_inherited_trial_env(tmp_path,provider):
    source=Path(os.environ.get('HERMES_SOURCE','/opt/hermes'))
    assert source.is_dir(),'pinned Hermes source required'
    home=tmp_path/'home';home.mkdir();(home/'config.yaml').write_text(connect._model_yaml_block(provider)+'\n')
    key_name='ANTHROPIC_API_KEY' if provider=='anthropic' else 'OPENAI_API_KEY'
    base='https://api.anthropic.com' if provider=='anthropic' else 'https://api.openai.com/v1'
    owner_key='synthetic-owner-key'
    if provider=='chatgpt':
        import base64,time
        from squire_codex_device import build_auth_json
        encode=lambda value:base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')
        owner_key=encode({'alg':'none'})+'.'+encode({'exp':int(time.time())+86400,'https://api.openai.com/auth':{'chatgpt_account_id':'synthetic-account','chatgpt_plan_type':'plus'}})+'.synthetic'
        (home/'auth.json').write_text(json.dumps(build_auth_json({'access_token':owner_key,'refresh_token':'synthetic-refresh'})))
        (home/'.env').write_text('')
    else:
        (home/'.env').write_text(key_name+'='+owner_key+'\n'+('ANTHROPIC_BASE_URL='+base+'\n' if provider=='anthropic' else ''))
    code='''import socket
def forbid_network(*args,**kwargs):raise AssertionError('provider network call forbidden')
socket.socket.connect=forbid_network
from pathlib import Path
from types import SimpleNamespace
from hermes_cli.env_loader import load_hermes_dotenv
load_hermes_dotenv(hermes_home=Path(__import__('os').environ['HERMES_HOME']))
from hermes_cli.runtime_provider import resolve_runtime_provider
from squire_overlay.adapter import resolved_owner_provider
r=resolve_runtime_provider()
assert r['api_key']==__import__('os').environ['EXPECTED_KEY']
assert resolved_owner_provider(SimpleNamespace(home=Path(__import__('os').environ['HERMES_HOME']),kind='dm'))==__import__('os').environ['EXPECTED_PROVIDER']
print('effective-owner-route-proven')
'''
    env={k:v for k,v in os.environ.items() if not any(x in k for x in ('API_KEY','TOKEN','SECRET','BASE_URL','HERMES_','ANTHROPIC','OPENAI','OPENROUTER'))}
    env.update(HERMES_HOME=str(home),HOME=str(home),HERMES_SKIP_CONFIG_MIGRATION='1',EXPECTED_PROVIDER=provider,EXPECTED_KEY=owner_key,ANTHROPIC_API_KEY='synthetic-trial-key',ANTHROPIC_BASE_URL='http://trial.invalid',PYTHONPATH=str(source)+os.pathsep+str(Path(__file__).resolve().parents[1]/'plugin'))
    result=subprocess.run([sys.executable,'-c',code],env=env,capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr[-1500:]
    assert result.stdout.strip()=='effective-owner-route-proven'
    if provider=='chatgpt':
        reopened=subprocess.run([sys.executable,'-c',code],env=env,capture_output=True,text=True,timeout=30)
        assert reopened.returncode==0,reopened.stderr[-1500:]
        assert reopened.stdout.strip()=='effective-owner-route-proven'


def test_pilot_celebration_does_not_waive_service_limits(scope):
    text=connect._celebration_text('chatgpt')
    assert 'limits stay the same' in text and 'caps no longer apply' not in text and '72' not in text


def test_concurrent_connect_posts_consume_one_nonce_once(scope,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from http.server import ThreadingHTTPServer
    import threading
    import time
    import urllib.parse
    import urllib.request
    spec=importlib.util.spec_from_file_location("squire_test_shim",BIN/'squire-webhook-shim.py')
    shim=importlib.util.module_from_spec(spec);spec.loader.exec_module(shim)
    stored=[];validated=threading.Event()
    def validate(*args):
        validated.set();time.sleep(.1);return True,"valid"
    monkeypatch.setattr(connect,'validate_key',validate)
    monkeypatch.setattr(connect,'store_api_key',lambda provider,key:stored.append(key))
    monkeypatch.setattr(connect,'run_connected_pipeline',lambda *args:True)
    nonce=connect.mint_nonce();server=ThreadingHTTPServer(('127.0.0.1',0),shim.Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def post(key):
        request=urllib.request.Request(f'http://127.0.0.1:{server.server_port}/connect/{nonce}',data=urllib.parse.urlencode({'provider':'openai','api_key':key}).encode())
        with urllib.request.urlopen(request,timeout=5) as response:return response.read().decode()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(post,'synthetic-first-key-long-enough');assert validated.wait(2)
            second=pool.submit(post,'synthetic-second-key-long-enough')
            pages=[first.result(),second.result()]
        assert stored==['synthetic-first-key-long-enough']
        assert sum('key is saved' in page for page in pages)==1
        assert sum("isn't live" in page for page in pages)==1
    finally:server.shutdown();server.server_close();thread.join()


def test_interrupted_connection_stays_explicitly_retryable_without_revocation(scope,monkeypatch):
    calls=[];monkeypatch.setattr(connect,'notify_control_api',lambda p:calls.append(p))
    connect.write_connection_status('connecting',provider='anthropic')
    status=connect.connection_status()
    assert 'fresh connection attempt' in status['detail']
    assert reconcile_provider(scope,ready(scope)) and calls==[]
