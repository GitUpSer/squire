"""Exercise both installed Codex readers without model or auth requests."""
import json
import os
from pathlib import Path
import socket
import tempfile
from unittest.mock import patch


def check(readers):
    with tempfile.TemporaryDirectory() as directory:
        home = Path(directory)
        default = home / '.codex'
        derived = home / 'derived'
        for path, token in ((default, 'cli-access'), (derived, 'derived-access')):
            path.mkdir()
            (path / 'auth.json').write_text(json.dumps({
                'auth_mode': 'chatgpt', 'tokens': {'access_token': token, 'account_id': 'synthetic-account'}}))
        original = (default / 'auth.json').read_bytes()
        with patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')):
            with patch.object(Path, 'home', return_value=home):
                for reader in readers:
                    for override, token in ((str(derived), 'derived-access'), ('', 'cli-access'), (None, 'cli-access')):
                        with patch.dict(os.environ):
                            if override is None:
                                os.environ.pop('CODEX_HOME', None)
                            else:
                                os.environ['CODEX_HOME'] = override
                            assert reader(None) == (token, 'synthetic-account')
                    with patch.dict(os.environ, CODEX_HOME=str(derived)):
                        for invalid in ({'tokens': {'access_token': 'invalid-mode'}},
                                        {'auth_mode': 'chatgpt', 'tokens': {}}):
                            (derived / 'auth.json').write_text(json.dumps(invalid))
                            try:
                                reader(None)
                            except ValueError:
                                pass
                            else:
                                raise AssertionError('invalid auth accepted')
                        (derived / 'auth.json').write_text(json.dumps({
                            'auth_mode': 'chatgpt', 'tokens': {'access_token': 'derived-access', 'account_id': 'synthetic-account'}}))
        assert (default / 'auth.json').read_bytes() == original
    print('Both founder Codex readers: override/default/validation/no-write passed')


if __name__ == '__main__':
    from importlib.metadata import version
    assert version('hindsight-api-slim') in {'0.5.2', '0.9.0'}
    from hindsight_api.engine.providers.codex_llm import CodexLLM
    from hindsight_api.engine.llm_wrapper import LLMProvider
    check((CodexLLM._load_codex_auth, LLMProvider._load_codex_auth))
