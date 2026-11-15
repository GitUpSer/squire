import base64
import io
from pathlib import Path
import zipfile
import pytest
from squire_overlay.browser_state import BrowserState

@pytest.fixture
def state(tmp_path,monkeypatch):
    monkeypatch.setenv('SQUIRE_DEK',base64.b64encode(b'b'*32).decode())
    monkeypatch.setenv('SQUIRE_CRYPTO_MODULE',str(Path(__file__).resolve().parents[2]/'bin/squire_secrets.py'))
    return BrowserState(tmp_path/'home','runtime-a',str(tmp_path))


def test_browser_cookie_state_encrypted_and_runtime_bound(state,tmp_path):
    profile=state.root/'profiles'/'user-hash'/'storage-state.json'
    profile.parent.mkdir(parents=True)
    profile.write_text('{"cookies":[{"value":"private-cookie-secret"}]}')
    state.save()
    assert b'private-cookie-secret' not in state.encrypted.read_bytes()
    restored=BrowserState(state.home,'runtime-a',str(tmp_path));restored.restore()
    assert (restored.root/'profiles/user-hash/storage-state.json').read_text()==profile.read_text()
    other=BrowserState(state.home,'runtime-b',str(tmp_path))
    with pytest.raises(Exception):other.restore()
    assert stat_mode(state.encrypted)==0o600


def stat_mode(path):return path.stat().st_mode & 0o777


def test_archive_cannot_write_outside_volatile_scope(state,tmp_path):
    raw=io.BytesIO()
    with zipfile.ZipFile(raw,'w') as archive:archive.writestr('../escape','secret')
    state.encrypted.write_bytes(state.codec.seal_bytes(state.key,raw.getvalue(),state.aad))
    with pytest.raises(ValueError):state.restore()
    assert not (tmp_path/'escape').exists()


def test_browser_snapshot_refuses_symlink_secret_escape(state,tmp_path):
    secret=tmp_path/'unrelated';secret.write_text('not-browser-state')
    (state.root/'profiles').mkdir()
    (state.root/'profiles/leak').symlink_to(secret)
    with pytest.raises(ValueError):state.save()


def test_group_runtime_never_starts_browser(monkeypatch):
    from squire_overlay import browser_state
    monkeypatch.setenv('SQUIRE_RUNTIME_KIND','group')
    monkeypatch.setattr(browser_state,'BrowserState',lambda *a,**k:pytest.fail('group touched browser state'))
    assert browser_state.run() is None


def test_browser_process_gets_no_model_or_messaging_credentials(monkeypatch):
    from squire_overlay.browser_state import browser_environment
    for key in ['SQUIRE_DEK','SQUIRE_EGRESS_TOKEN','SQUIRE_CONTROL_TOKEN','ANTHROPIC_API_KEY','OPENAI_API_KEY']:
        monkeypatch.setenv(key,'must-not-reach-browser')
    monkeypatch.setenv('CAMOFOX_API_KEY','browser-only-key')
    env=browser_environment()
    assert 'must-not-reach-browser' not in env.values()
    assert env['CAMOFOX_API_KEY']=='browser-only-key'
