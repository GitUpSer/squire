"""The existing access-only export must bind account identity to its selected JWT."""
import base64
import json
import os
from pathlib import Path
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'bin/squire-hindsight-env.sh'


def token(claim):
    payload = json.dumps({'exp': 2000000000, 'https://api.openai.com/auth': claim}).encode()
    return 'header.' + base64.urlsafe_b64encode(payload).decode().rstrip('=') + '.signature'


@pytest.mark.parametrize('access,explicit,expected', [
    (token({'chatgpt_account_id': 'jwt-account'}), None, 'jwt-account'),
    (token({'chatgpt_account_id': 'jwt-account'}), 'explicit-account', 'explicit-account'),
    (token({}), None, ''),
    (token({'chatgpt_account_id': 123}), None, ''),
    (token(None), None, ''),
    ('malformed-jwt', None, ''),
])
def test_selected_token_account_export(tmp_path, access, explicit, expected):
    (tmp_path / '.env').write_text('')
    selected = {'access_token': access, 'refresh_token': 'never-export-refresh'}
    if explicit is not None:
        selected['account_id'] = explicit
    document = {'providers': {'openai-codex': {'tokens': {'access_token': 'old', 'account_id': 'other-account'}}},
                'credential_pool': {'openai-codex': [{'tokens': selected}]}}
    # Malformed tokens tie at zero expiry; keep only this candidate for that case.
    if access == 'malformed-jwt':
        document['providers'] = {}
    source = tmp_path / 'auth.json'
    source.write_text(json.dumps(document))
    original = source.read_bytes()
    env = dict(os.environ, SQUIRE_SECRETS_TMPFS=str(tmp_path), SQUIRE_CODEX_AUTH_FILE=str(source),
               SQUIRE_CODEX_HOME_DIR=str(tmp_path / 'derived'),
               SQUIRE_HINDSIGHT_ENV_FILE=str(tmp_path / 'hindsight.env'), SQUIRE_PYTHON='python3')
    result = subprocess.run(['bash', str(SCRIPT)], env=env, capture_output=True, text=True)
    assert result.returncode == 10, result.stderr
    derived = json.loads((tmp_path / 'derived/auth.json').read_text())
    assert derived == {'auth_mode': 'chatgpt', 'tokens': {'access_token': access, 'account_id': expected}}
    assert source.read_bytes() == original
    assert 'never-export-refresh' not in result.stdout + result.stderr
    assert (tmp_path / 'derived/auth.json').stat().st_mode & 0o777 == 0o600
    assert subprocess.run(['bash', str(SCRIPT)], env=env, capture_output=True).returncode == 0
